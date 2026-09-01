#!/usr/bin/env python3
"""
funding_crop.py - 罗海资金费率收割:候选扫描 + 执行开仓

用法:
    python3 funding_crop.py scan    [--wait-until-second=55]
    python3 funding_crop.py open    [--wait-until-second=1]
"""
import sys, os, json, time, hmac, hashlib, base64, argparse, math
from datetime import datetime, timezone, timedelta
from pathlib import Path

# 先加载 .env 再到 config(直接内联,不用load_env.py绕弯)
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

# ─── TG 通知 ──────────────────────────────────────────────────────
def tg_send(text: str):
    if not TG_BOT_TOKEN:
        print(f"[TG] (未配置bot) {text[:200]}")
        return
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=10)
    except Exception as e:
        print(f"[TG ERR] {e}")


# ─── 今日开仓总量统计(每日00:00按日期自然重置) ────────────────
def bump_daily_open_count(add: int = 0) -> int:
    """今日开仓累计入库。
    读 daily_open_count.json, 若记录的日期不是今天则重置为 0(实现每日00:00自动归零),
    加上本次 add 后写回, 返回【今日累计开仓总量】。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    cur = 0
    try:
        if os.path.exists(DAILY_OPEN_COUNT_FILE):
            with open(DAILY_OPEN_COUNT_FILE) as f:
                d = json.load(f)
            if d.get("date") == today:
                cur = int(d.get("count", 0))
            # 日期不同 → 视为跨天, cur 保持 0, 自动重置
    except Exception as e:
        print(f"(读今日开仓统计异常: {e})")
    cur += add
    try:
        with open(DAILY_OPEN_COUNT_FILE, "w") as f:
            json.dump({"date": today, "count": cur}, f)
    except Exception as e:
        print(f"(写今日开仓统计异常: {e})")
    return cur

# ─── 时间对齐 ──────────────────────────────────────────────────────
def wait_until_second(target_second: int, max_wait=65):
    """阻塞直到当前分钟的 target_second 秒,返回实际等待秒数"""
    now = datetime.now()
    target = now.replace(second=target_second, microsecond=0)
    if target <= now:
        target += timedelta(minutes=1)
    delta = (target - now).total_seconds()
    if delta > max_wait:
        print(f"[WAIT] 目标 {target_second}s 需等 {delta:.0f}s,超过 {max_wait}s,跳过")
        return False
    print(f"[WAIT] 等待 {delta:.1f}s 到 {target.strftime('%H:%M:%S')}...")
    time.sleep(delta)
    return True

# ─── 交易所 ────────────────────────────────────────────────────────
def get_exchange():
    ex = ccxt.bitget({
        "apiKey": API_KEY,
        "secret": API_SECRET,
        "password": API_PASS,
        "options": {"defaultType": "swap"},
        "enableRateLimit": True,
    })
    return ex

def bitget_v2_get(path: str, method: str = "GET", body: str = "") -> dict:
    """带签名的 Bitget V2 请求(支持 GET/POST)"""
    t = str(int(time.time() * 1000))
    msg = t + method + path + body
    sig = base64.b64encode(hmac.new(API_SECRET.encode(), msg.encode(), hashlib.sha256).digest()).decode()
    hdrs = {
        "ACCESS-KEY": API_KEY, "ACCESS-SIGN": sig,
        "ACCESS-TIMESTAMP": t, "ACCESS-PASSPHRASE": API_PASS,
        "Content-Type": "application/json",
    }
    url = "https://api.bitget.com" + path
    if method == "GET":
        r = requests.get(url, headers=hdrs, timeout=15)
    else:
        r = requests.post(url, headers=hdrs, data=body, timeout=15)
    return r.json()

# ─── 带单限制币黑名单(自学习) ────────────────────────────────
def load_copy_blacklist() -> set:
    """读取带单限制币黑名单(base 币大写,如 SYN/HYPER)"""
    try:
        if os.path.exists(COPY_TRADE_BLACKLIST_FILE):
            with open(COPY_TRADE_BLACKLIST_FILE) as f:
                data = json.load(f)
            return set(data.get("symbols", []))
    except Exception as e:
        print(f"(读取黑名单异常: {e})")
    return set()


def save_copy_blacklist(syms: set):
    """持久化带单限制币黑名单"""
    try:
        with open(COPY_TRADE_BLACKLIST_FILE, "w") as f:
            json.dump({"updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "symbols": sorted(syms)}, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"(写入黑名单异常: {e})")


def is_copy_trade_error(e) -> bool:
    """判断异常是否为带单交易限制(code 40020 / 40731,或文案含 copy trading)"""
    s = str(e)
    if "copy trading" in s.lower() or "copy-trade" in s.lower():
        return True
    # ccxt/bitget 错误可能带 code
    for code in ("40020", "40731"):
        if code in s:
            return True
    return False


# ─── 阶段一:扫描(Bitget 费率源 → Bitget 同所开仓,双向) ────────
def cmd_scan(wait_second=40):
    """从【Bitget 自己】拉资金费率,绝对值≥阈值就开【双向仓】(同币多空各一单)。
    多空各一单 → 两边都吃跟踪止盈,方向怎么走总有一边触发平仓。
    """
    src = "Bitget" if FUNDING_SOURCE == "bitget" else "币安"
    print(f"[SCAN] 开始扫描({src}费率→Bitget双向开仓),等待到 {wait_second} 秒...")
    wait_until_second(wait_second)

    try:
        ex = get_exchange()

        # ── 1. 拉 Bitget 全永续资金费率 ──
        g_funding = ex.fetch_funding_rates()

        # ── 2. 拉合约列表(校验可用性 + 唯一 base) ──
        g_markets = ex.load_markets()
        print(f"[SCAN] Bitget 合约符号数: {len(g_markets)}")

        now_utc = datetime.now(timezone.utc)
        now_epoch = int(now_utc.timestamp() * 1000)
        candidates = []

        # 带单限制币黑名单(自学习)
        copy_black = load_copy_blacklist()
        if copy_black:
            print(f"[SCAN] 带单限制黑名单 {len(copy_black)} 个: {sorted(copy_black)}")

        for sym, d in g_funding.items():
            if not sym.endswith("/USDT:USDT"):
                continue
            base = sym.split("/")[0].upper()

            # 排除 BTC/ETH/BNB
            if any(base.upper().startswith(e.upper()) for e in EXCLUDE_SYMBOLS):
                continue
            # 带单限制币:跳过
            if base.upper() in copy_black:
                continue

            rate = d.get("fundingRate")
            if rate is None or abs(rate) < FUNDING_THRESHOLD:
                continue

            fund_dt = now_utc
            ts = d.get("fundingTimestamp")
            if isinstance(ts, (int, float)) and ts:
                fund_dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)

            # 双向模式: 不管正负, 同币都开 多+空 各一单
            side = "long" if rate > 0 else "short"
            bg_sym = base + "USDT"
            candidates.append({
                "symbol": bg_sym,
                "rate": rate,
                "side": side,          # 主方向(仅展示用,实际双向都开)
                "interval_h": 8,
                "next_settle_ts": int(ts) if isinstance(ts, (int, float)) and ts else now_epoch,
                "next_settle_str": fund_dt.strftime("%H:%M:%S UTC"),
                "countdown_ms": 0,
                "source_pair": sym,
            })

        if candidates:
            with open(CANDIDATES_FILE, "w") as f:
                json.dump({"ts": now_epoch, "candidates": candidates}, f, indent=2)
            print(f"[SCAN] ✅ {len(candidates)} 个候选,已写入 {CANDIDATES_FILE}(Bitget费率→双向开仓)")
            for c in candidates:
                print(f"   {c['symbol']:<22} 双向(多+空) 费率={c['rate']*100:.4f}%")
            msg = f"💰 Bitget费率候选 {len(candidates)} 个(双向多+空):\n"
            for c in candidates:
                msg += f"• {c['symbol']} 多+空 费率={c['rate']*100:.4f}%\n"
            tg_send(msg)
        else:
            print("[SCAN] ❌ 零候选")
            if os.path.exists(CANDIDATES_FILE):
                os.remove(CANDIDATES_FILE)
            tg_send("⚠️ 本轮 Bitget 费率扫描无候选")

    except Exception as e:
        print(f"[SCAN ERROR] {e}")
        import traceback; traceback.print_exc()
        tg_send(f"❌ 扫描异常: {e}")

# ─── 阶段二:开仓 ──────────────────────────────────────────────────
def cmd_open(wait_second=None):
    """执行开仓(双向模式): 满足条件的币 多空各开一单, 已有对则跳过, 只有一边则补齐。
    双向: hedged=True。每单初始保证金 SINGLE_AMOUNT, 杠杆 LEVERAGE, 全仓。
    止盈止损: ±90% 挂计划单(reduceOnly)。移动止盈由 tracker 负责。
    """
    if wait_second is not None and wait_second > 0:
        print(f"[OPEN] 开始开仓,等待到 {wait_second} 秒...")
        wait_until_second(wait_second)

    # 读候选
    if not os.path.exists(CANDIDATES_FILE):
        print("[OPEN] ❌ 无候选文件,跳过")
        return

    with open(CANDIDATES_FILE) as f:
        data = json.load(f)
    candidates = data.get("candidates", [])
    if not candidates:
        print("[OPEN] ❌ 候选文件为空,跳过")
        os.remove(CANDIDATES_FILE)
        return

    print(f"[OPEN] 候选 {len(candidates)} 个,开始开仓(双向 多+空)...")

    try:
        ex = get_exchange()

        # ── 设置为双向持仓模式 (hedged=True) ──
        try:
            ex.set_position_mode(hedged=True)
            print(f"[OPEN] ✅ 已设置双向持仓模式 (hedged)")
        except Exception as e:
            print(f"[OPEN] ⚠️ 设置双向持仓模式异常: {e}")

        # ── 拉当前持仓, 按 (symbol, side) 精确记录, 而非仅 symbol ──
        existing = {}   # {ccxt_sym: {"long": bool, "short": bool}}
        try:
            positions = ex.fetch_positions()
            for p in positions:
                if p.get("contracts") and float(p["contracts"]) > 0:
                    s = p["symbol"]
                    side = p.get("side")  # long/short
                    existing.setdefault(s, {"long": False, "short": False})
                    if side in ("long", "short"):
                        existing[s][side] = True
        except Exception as e:
            print(f"[OPEN] 拉持仓失败(继续): {e}")

        opened = []
        skipped = []

        for cand in candidates:
            sym_raw = cand["symbol"]
            base = sym_raw[:-4] if sym_raw.endswith("USDT") else sym_raw
            ccxt_sym = base + "/USDT:USDT"

            # 排除 BTC/ETH/BNB
            if any(sym_raw.upper().startswith(e.upper()) for e in EXCLUDE_SYMBOLS):
                print(f"   ⏭ {sym_raw} 排除币种,跳过")
                continue

            # 双向模式: 要开的方向 = 多 + 空(成对)
            want_sides = ["long", "short"]

            # 实时检查该币当前两边持仓情况(防止数据延迟)
            have = {"long": False, "short": False}
            try:
                live_pos = ex.fetch_positions([ccxt_sym])
                for lp in live_pos:
                    if lp.get("contracts") and float(lp["contracts"]) > 0:
                        sd = lp.get("side")
                        if sd in ("long", "short"):
                            have[sd] = True
            except Exception:
                pass
            # 合并缓存 + 实时
            rec = existing.get(ccxt_sym, {"long": False, "short": False})
            for k in ("long", "short"):
                have[k] = have[k] or rec[k]

            # 决定这一轮要补哪些方向
            to_open = []
            for sd in want_sides:
                if not have[sd]:
                    to_open.append(sd)
                else:
                    print(f"   ⏭ {ccxt_sym} {sd} 已有持仓,跳过该方向")

            if not to_open:
                print(f"   ⏭ {ccxt_sym} 多空两向都已有持仓,整币跳过")
                skipped.append(ccxt_sym)
                continue

            # 获取合约信息(精度/最小量)一次, 供两边共用
            try:
                ex.load_markets()
                market = ex.market(ccxt_sym)
            except Exception as e:
                print(f"   ❌ {ccxt_sym} 获取合约信息失败: {str(e)[:70]}")
                continue
            prec = int(market.get("precision", {}).get("amount", 1))
            size_multiplier = float(market.get("info", {}).get("sizeMultiplier", "1"))
            min_trade = float(market.get("info", {}).get("minTradeNum", "0"))
            min_usdt = float(market.get("info", {}).get("minTradeUSDT", "5"))
            pp = int((market.get("info") or {}).get("pricePlace", "6"))

            # 当前市价(供计算数量 + 止盈止损触发价)
            try:
                ticker = ex.fetch_ticker(ccxt_sym)
                price = ticker.get("last", 0)
            except Exception as e:
                print(f"   ❌ {ccxt_sym} 拉行情失败: {str(e)[:70]}")
                continue
            if price <= 0:
                print(f"   ❌ {ccxt_sym} 价格无效")
                continue

            # 数量 = 保证金*杠杆 / 价格, 按精度 floor
            qty = (SINGLE_AMOUNT * LEVERAGE) / price
            factor = 10 ** prec
            qty = math.floor(qty * factor) / factor
            if qty < min_trade:
                print(f"   ❌ {ccxt_sym} 数量={qty} 低于最小交易量 {min_trade}")
                continue
            if qty * price < min_usdt:
                print(f"   ❌ {ccxt_sym} 成交额 {qty*price:.2f}U 低于最低 {min_usdt}U")
                continue

            # ── 开仓前: 双向模式先按方向设好保证金模式+杠杆(修复45117)。 ──
            #    真凶: create_order 报文里的 marginMode 字段。当某方向已有持仓/订单时,
            #    Bitget 会把带 marginMode 的下单当作"调整保证金模式"动作拒绝(45117)。
            #    修复: 开仓前用带 side 的 set_margin_mode 把缺失方向预切到目标模式,
            #    create_order 不再传 marginMode(保持当前 mode, 不触发切换动作)。
            for side in to_open:
                for _try in range(2):
                    try:
                        ex.set_margin_mode(MARGIN_MODE, ccxt_sym, {"side": side})
                        break
                    except Exception as e:
                        # 再试一次不带 side(部分场景首次需先建立方向)
                        try:
                            ex.set_margin_mode(MARGIN_MODE, ccxt_sym)
                            break
                        except Exception as e2:
                            if _try == 0:
                                print(f"   ⚠️ {ccxt_sym} 开仓前切全仓({side})失败(拟继续): {str(e2)[:60]}")
                try:
                    ex.set_leverage(LEVERAGE, ccxt_sym, {"side": side})
                except Exception:
                    try:
                        ex.set_leverage(LEVERAGE, ccxt_sym)
                    except Exception as e:
                        print(f"   ⚠️ {ccxt_sym} 设杠杆({side})失败: {str(e)[:60]}")

            # ── 第一步: 把所有缺失方向都开出来(双向多用 create_order + hedged=True + tradeSide=Open) ──
            just_opened = []   # 本轮新开的方向
            newly_summary = {} # {side: order_id}
            for side in to_open:
                dside = "buy" if side == "long" else "sell"
                try:
                    order = ex.create_order(ccxt_sym, "market", dside, float(qty), None, {
                        "hedged": True,
                        "productType": "USDT-FUTURES",
                    })
                    if not (order and order.get("id")):
                        print(f"   ❌ {ccxt_sym} {side} 下单失败: {order}")
                        continue
                    order_id = order["id"]
                    newly_summary[side] = order_id
                    just_opened.append(side)
                    print(f"   ✅ {ccxt_sym} {side}({dside}) {qty}张 @ {price} orderId={order_id}")
                    time.sleep(0.5)
                except Exception as e:
                    print(f"   ❌ {ccxt_sym} {side} 开仓异常: {str(e)[:90]}")
                    if is_copy_trade_error(e):
                        b = base.upper()
                        black = load_copy_blacklist()
                        if b not in black:
                            black.add(b)
                            save_copy_blacklist(black)
                            print(f"   🚫 {ccxt_sym} 带单限制币,已加入黑名单: {b}")
                            tg_send(f"🚫 {ccxt_sym} 带单限制币,已加入跳过黑名单")
                    else:
                        tg_send(f"⚠️ {ccxt_sym} {side} 开仓异常: {str(e)[:80]}")

            if not just_opened:
                print(f"   ⚠️ {ccxt_sym} 本轮没有新开成任何方向")
                continue

            # ── 第二步: 全部开完后按方向补切全仓 + 确认5x (带side,避免对已持仓方向报45117) ──
            time.sleep(1.0)
            for ls in just_opened:
                try:
                    ex.set_margin_mode(MARGIN_MODE, ccxt_sym, {"side": ls})
                    print(f"   🔄 {ccxt_sym} 补切全仓({ls})")
                except Exception:
                    try:
                        ex.set_margin_mode(MARGIN_MODE, ccxt_sym)
                        print(f"   🔄 {ccxt_sym} 补切全仓(整体)")
                    except Exception as e:
                        print(f"   ⚠️ {ccxt_sym} 补切全仓({ls})异常: {str(e)[:60]}")
            for ls in just_opened:
                for lr in range(3):
                    try:
                        ex.set_leverage(LEVERAGE, ccxt_sym, {"side": ls})
                        break
                    except Exception:
                        time.sleep(0.5)

            # ── 第三步: 回读确认每个新开方向的 marginMode + 杠杆 ──
            time.sleep(1.0)
            confirmed = {}   # side -> {mm_ok, lev_ok}
            for rl in range(6):
                try:
                    chk = ex.fetch_positions([ccxt_sym])
                except Exception:
                    chk = []
                for cp in chk:
                    cs = cp.get("side")
                    if cs in just_opened and cp.get("contracts") and float(cp["contracts"]) > 0:
                        amm = str(cp.get("marginMode") or "").lower()
                        mm_ok = amm in ("cross", "crossed")
                        lev = float(cp.get("leverage") or 0)
                        lev_ok = abs(lev - LEVERAGE) < 1e-6
                        if cs not in confirmed or (mm_ok and lev_ok):
                            confirmed[cs] = {"mm_ok": mm_ok, "lev_ok": lev_ok, "mm": amm, "lev": lev}
                if len(confirmed) >= len(just_opened) and all(v["mm_ok"] and v["lev_ok"] for v in confirmed.values()):
                    break
                time.sleep(1.0)
            for cs in just_opened:
                c = confirmed.get(cs, {"mm_ok": False, "lev_ok": False, "mm": "?", "lev": "?"})
                if c["mm_ok"] and c["lev_ok"]:
                    print(f"   ✅ {ccxt_sym} {cs} 全仓+{LEVERAGE}x 确认")
                else:
                    print(f"   ⚠️ {ccxt_sym} {cs} mm={c['mm']} lev={c['lev']} (可能未切对)")
                    tg_send(f"⚠️ {ccxt_sym} {cs} 补切异常 mm={c['mm']} lev={c['lev']}, 请人工核")

            # ── 第四步: 对每个新开仓位只挂止损(不挂止盈,止盈交给移动跟踪止盈 tracker)。2026-09-01 娜姐要求 ──
            for side in just_opened:
                entry_px = price
                if side == "long":
                    sl_px = price * (1 - SL_LONG_PCT)      # ×0.01
                else:
                    sl_px = price * (1 + SL_SHORT_PCT)     # ×3.99
                reduce_side = "sell" if side == "long" else "buy"
                try:
                    trig_r = math.floor(sl_px * (10 ** pp)) / (10 ** pp)
                    tord = ex.create_trigger_order(
                        ccxt_sym, "market", reduce_side, float(qty), None, trig_r, {
                            "hedged": True,
                            "productType": "USDT-FUTURES",
                            "reduceOnly": True,
                        }
                    )
                    if tord and tord.get("id"):
                        print(f"   🛡️ {ccxt_sym} {side} SL @ {trig_r}")
                    else:
                        print(f"   ⚠️ {ccxt_sym} {side} SL 下单返回异常: {tord}")
                except Exception as e:
                    print(f"   ⚠️ {ccxt_sym} {side} SL 设置失败: {str(e)[:80]}")

                mmc = confirmed.get(side, {}).get("mm_ok", False)
                opened.append({
                    "symbol": ccxt_sym,
                    "side": side,
                    "qty": qty,
                    "price": price,
                    "amount_usdt": SINGLE_AMOUNT,
                    "rate": cand["rate"],
                    "order_id": newly_summary.get(side),
                    "margin_mode": MARGIN_MODE,
                    "margin_mode_confirmed": mmc,
                    "leverage": confirmed.get(side, {}).get("lev", LEVERAGE),
                })
                existing.setdefault(ccxt_sym, {"long": False, "short": False})[side] = True

        # 日志
        log_entry = {
            "ts": int(time.time() * 1000),
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "mode": "hedged",
            "config": {"margin_mode": MARGIN_MODE, "leverage": LEVERAGE, "amount_usdt": SINGLE_AMOUNT},
            "candidates_count": len(candidates),
            "opened": opened,
            "skipped": skipped,
        }
        os.makedirs(LOG_DIR, exist_ok=True)
        log_file = os.path.join(LOG_DIR, f"open_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
        with open(log_file, "w") as f:
            json.dump(log_entry, f, indent=2)

        # TG
        if opened:
            daily_total = bump_daily_open_count(len(opened))
            msg = "🚀 双向开仓:\n"
            for o in opened:
                mm_tag = "✅全仓" if (o.get("margin_mode_confirmed") and MARGIN_MODE=="crossed") else "⚠️" + str(o.get("actual_margin_mode","?"))
                msg += f"• {o['symbol']} {o['side']} {o['qty']:.4f}张 @ {o['price']:.6f} [{mm_tag}]\n"
            msg += f"  (保证金 {SINGLE_AMOUNT}U×{LEVERAGE}x {MARGIN_MODE}/单, 止损止盈±90%)"
            msg += f"\n📊 今日开仓总量: {daily_total} 个"
            tg_send(msg)
        if skipped:
            tg_send(f"⏭ 跳过完整双边持仓: {', '.join(skipped[:5])}{'...' if len(skipped)>5 else ''}")

        os.remove(CANDIDATES_FILE)
        print(f"[OPEN] ✅ 完成,删除候选文件,共开 {len(opened)} 笔")

    except Exception as e:
        print(f"[OPEN ERROR] {e}")
        import traceback; traceback.print_exc()
        tg_send(f"❌ 开仓异常: {e}")


# ─── 主入口 ────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="罗海资金费率收割")
    parser.add_argument("action", choices=["scan", "open"], help="scan=扫候选, open=执行开仓")
    parser.add_argument("--wait-until-second", type=int, default=None, help="等待到指定秒数再执行")
    parser.add_argument("--auto-open", action="store_true", help="scan 完成后等待整点自动执行 open")
    args = parser.parse_args()

    if args.action == "scan":
        wait_sec = args.wait_until_second if args.wait_until_second is not None else 40
        cmd_scan(wait_second=wait_sec)
        if args.auto_open:
            # 满足条件后直接开仓,不再等待整点+2s
            if os.path.exists(CANDIDATES_FILE):
                print("[AUTO-OPEN] 有候选,满足条件直接开仓...")
                cmd_open(wait_second=None)
            else:
                print("[AUTO-OPEN] 无候选文件,跳过")
    elif args.action == "open":
        cmd_open(wait_second=None)
