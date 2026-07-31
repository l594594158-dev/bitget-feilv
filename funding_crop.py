#!/usr/bin/env python3
"""
funding_crop.py — Bitget 资金费率反弹策略：候选扫描 + 执行开仓

用法:
    python3 funding_crop.py scan    [--wait-until-second=55]
    python3 funding_crop.py open    [--wait-until-second=1]
"""
import sys, os, json, time, hmac, hashlib, base64, argparse, math
from datetime import datetime, timezone, timedelta
from pathlib import Path

# 先加载 .env 再到 config（直接内联，不用load_env.py绕弯）
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

# ─── 时间对齐 ──────────────────────────────────────────────────────
def wait_until_second(target_second: int, max_wait=65):
    """阻塞直到当前分钟的 target_second 秒，返回实际等待秒数"""
    now = datetime.now()
    target = now.replace(second=target_second, microsecond=0)
    if target <= now:
        target += timedelta(minutes=1)
    delta = (target - now).total_seconds()
    if delta > max_wait:
        print(f"[WAIT] 目标 {target_second}s 需等 {delta:.0f}s，超过 {max_wait}s，跳过")
        return False
    print(f"[WAIT] 等待 {delta:.1f}s 到 {target.strftime('%H:%M:%S')}…")
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
    """带签名的 Bitget V2 请求（支持 GET/POST）"""
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

# ─── 阶段一：扫描 ──────────────────────────────────────────────────
def cmd_scan(wait_second=55):
    print(f"[SCAN] 开始扫描，等待到 {wait_second} 秒…")
    wait_until_second(wait_second)

    try:
        ex = get_exchange()
        # 1. 批量获取所有 ticker（含费率）
        tickers_raw = bitget_v2_get("/api/v2/mix/market/tickers?productType=USDT-FUTURES")
        if tickers_raw.get("code") != "00000":
            raise Exception(f"tickers API 失败: {tickers_raw.get('msg')}")

        # 2. 批量获取所有合约配置（含 fundInterval）
        contracts_raw = bitget_v2_get("/api/v2/mix/market/contracts?productType=USDT-FUTURES")
        if contracts_raw.get("code") != "00000":
            raise Exception(f"contracts API 失败: {contracts_raw.get('msg')}")

        contracts_map = {}
        for c in contracts_raw.get("data", []):
            sym = c.get("symbol", "")
            contracts_map[sym] = c

        now_utc = datetime.now(timezone.utc)
        now_epoch = int(now_utc.timestamp() * 1000)
        candidates = []

        for t in tickers_raw.get("data", []):
            sym = t.get("symbol", "")
            # 排除
            skip = False
            for ex_sym in EXCLUDE_SYMBOLS:
                if sym.upper().startswith(ex_sym.upper()):
                    skip = True
                    break
            if skip:
                continue

            rate_str = t.get("fundingRate", "0")
            rate = float(rate_str) if rate_str else 0.0

            # 筛选费率绝对值 ≥ 阈值
            if abs(rate) < FUNDING_THRESHOLD:
                continue

            # 结算周期
            contract = contracts_map.get(sym, {})
            interval_h = int(contract.get("fundInterval", 8))
            interval_ms = interval_h * 3600 * 1000

            # 推算下次结算时间戳
            day_start = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
            elapsed_ms = (now_utc - day_start).total_seconds() * 1000
            next_interval_ms = ((elapsed_ms // interval_ms) + 1) * interval_ms
            next_settle_ts = int(day_start.timestamp() * 1000) + int(next_interval_ms)
            countdown_ms = next_settle_ts - now_epoch

            # 筛选：距结算 < 60s
            if countdown_ms > SETTLE_WINDOW * 1000 or countdown_ms < -2000:
                continue

            side = "long" if rate > 0 else "short"
            candidates.append({
                "symbol": sym,
                "rate": rate,
                "side": side,
                "interval_h": interval_h,
                "next_settle_ts": next_settle_ts,
                "next_settle_str": datetime.fromtimestamp(next_settle_ts/1000, tz=timezone.utc).strftime("%H:%M:%S UTC"),
                "countdown_ms": countdown_ms,
            })

        # 写入候选文件
        if candidates:
            with open(CANDIDATES_FILE, "w") as f:
                json.dump({"ts": now_epoch, "candidates": candidates}, f, indent=2)
            print(f"[SCAN] ✅ {len(candidates)} 个候选，已写入 {CANDIDATES_FILE}")
            for c in candidates:
                print(f"   {c['symbol']:<22} {c['side']:<6} 费率={c['rate']*100:.4f}%  倒计时={c['countdown_ms']/1000:.1f}s  周期={c['interval_h']}h")
            # 也推 TG
            msg = f"💰 费率候选 {len(candidates)} 个:\n"
            for c in candidates:
                msg += f"• {c['symbol']} {c['side']} {c['rate']*100:.4f}% ({c['interval_h']}h)\n"
            tg_send(msg)
        else:
            print("[SCAN] ❌ 零候选")
            # 删旧候选
            if os.path.exists(CANDIDATES_FILE):
                os.remove(CANDIDATES_FILE)
            tg_send("⚠️ 本轮费率扫描无候选")

    except Exception as e:
        print(f"[SCAN ERROR] {e}")
        import traceback; traceback.print_exc()
        tg_send(f"❌ 扫描异常: {e}")

# ─── 阶段二：开仓 ──────────────────────────────────────────────────
def cmd_open(wait_second=None):
    if wait_second is not None and wait_second > 0:
        print(f"[OPEN] 开始开仓，等待到 {wait_second} 秒…")
        wait_until_second(wait_second)

    # 读候选
    if not os.path.exists(CANDIDATES_FILE):
        print("[OPEN] ❌ 无候选文件，跳过")
        return

    with open(CANDIDATES_FILE) as f:
        data = json.load(f)
    candidates = data.get("candidates", [])
    if not candidates:
        print("[OPEN] ❌ 候选文件为空，跳过")
        os.remove(CANDIDATES_FILE)
        return

    print(f"[OPEN] 候选 {len(candidates)} 个，开始开仓…")

    try:
        ex = get_exchange()

        # 设置账户为单向持仓模式
        try:
            ex.set_position_mode(hedged=False)
            print(f"[OPEN] ✅ 已设置单向持仓模式")
        except Exception as e:
            print(f"[OPEN] ⚠️ 设置持仓模式（可忽略）: {e}")

        # 先拉一次当前持仓（symbol 列表）
        existing_positions = set()
        try:
            positions = ex.fetch_positions()
            for p in positions:
                if p.get("contracts") and float(p["contracts"]) > 0:
                    existing_positions.add(p["symbol"])
        except Exception as e:
            print(f"[OPEN] 拉持仓失败（继续）: {e}")

        opened = []
        skipped = []

        for cand in candidates:
            sym_raw = cand["symbol"]
            base = sym_raw[:-4] if sym_raw.endswith("USDT") else sym_raw  # 去掉尾部的 USDT
            ccxt_sym = base + "/USDT:USDT"  # ccxt swap 合约格式

            # 排除 BTC/ETH/BNB
            skip_ex = False
            for ex_sym in EXCLUDE_SYMBOLS:
                if sym_raw.upper().startswith(ex_sym.upper()):
                    print(f"   ⏭ {sym_raw} 排除币种，跳过")
                    skip_ex = True
                    break
            if skip_ex:
                continue

            # 检查已有持仓（同币种去重）
            if ccxt_sym in existing_positions:
                print(f"   ⏭ {ccxt_sym} 已有持仓，跳过")
                skipped.append(ccxt_sym)
                continue

            # 实时检查同币种是否已有未平仓位（防止数据延迟导致重复开仓）
            try:
                live_pos = ex.fetch_positions([ccxt_sym])
                for lp in live_pos:
                    if lp.get("contracts") and float(lp["contracts"]) > 0:
                        print(f"   ⏭ {ccxt_sym} 实时检查有持仓，跳过")
                        skipped.append(ccxt_sym)
                        existing_positions.add(ccxt_sym)
                        break
            except:
                pass

            if ccxt_sym in skipped:
                continue

            side = "buy" if cand["side"] == "long" else "sell"
            amount_usdt = SINGLE_AMOUNT

            try:
                # ── 设置杠杆 ────────────────────────────────────────
                leverage_ok = False
                for lev_retry in range(3):
                    try:
                        ex.set_leverage(LEVERAGE, ccxt_sym)
                        # ccxt没返回杠杆值，直接确认通过（set_leverage成功=OK）
                        leverage_ok = True
                        print(f"   ✅ {ccxt_sym} 杠杆已设置 {LEVERAGE}x")
                        break
                    except Exception as e:
                        err_str = str(e)
                        print(f"   ⚠️ {ccxt_sym} 设置杠杆失败: {err_str[:60]}，重试 ({lev_retry+1}/3)")
                    time.sleep(0.5)

                if not leverage_ok:
                    print(f"   ❌ {ccxt_sym} 杠杆设置失败（期望 {LEVERAGE}x），跳过此币种")
                    tg_send(f"⚠️ {ccxt_sym} 开仓跳过：杠杆设置失败，期望 {LEVERAGE}x")
                    continue

                # 设置保证金模式（关键：必须在该币无持仓/无挂单时才能切换成功；
                # 失败不影响后续下单，下单参数里会再次强制 marginMode）
                try:
                    ex.set_margin_mode(MARGIN_MODE, ccxt_sym)
                except Exception as e:
                    print(f"   ⚠️ {ccxt_sym} set_margin_mode 失败(继续下单，下单参数会强制): {str(e)[:80]}")

                # 获取当前市价，计算数量
                ticker = ex.fetch_ticker(ccxt_sym)
                price = ticker.get("last", 0)
                if price <= 0:
                    print(f"   ❌ {ccxt_sym} 价格无效")
                    continue

                # 数量 = 保证金 * 杠杆 / 价格
                qty = (amount_usdt * LEVERAGE) / price

                # 获取合约精度
                market = ex.market(ccxt_sym)
                prec = int(market.get("precision", {}).get("amount", 1))
                size_multiplier = float(market.get("info", {}).get("sizeMultiplier", "1"))
                min_trade = float(market.get("info", {}).get("minTradeNum", "0"))

                # 按精度舍入
                factor = 10 ** prec
                qty = math.floor(qty * factor) / factor

                # 确保不低于最小交易量
                if qty < min_trade:
                    print(f"   ❌ {ccxt_sym} 数量={qty} 低于最小交易量 {min_trade}")
                    continue

                # 确保成交额 ≥ 最低成交额限制
                notional_check = qty * price
                min_usdt = float(market.get("info", {}).get("minTradeUSDT", "5"))
                if notional_check < min_usdt:
                    print(f"   ❌ {ccxt_sym} 成交额 {notional_check:.2f}U 低于最低 {min_usdt}U")
                    continue

                # ccxt 下单 + 附带止损（marginMode 在参数里强制全仓/逐仓，不依赖 set_margin_mode）
                sl_price = price * 0.2 if cand["side"] == "long" else price * 1.8
                order = ex.create_order(ccxt_sym, "market", side, float(qty), None, {
                    "marginMode": MARGIN_MODE,
                    "productType": "USDT-FUTURES",
                    "stopLoss": {"triggerPrice": round(sl_price, 6)},
                })
                if order and order.get("id"):
                    order_id = order["id"]
                    print(f"   ✅ {ccxt_sym} {side} {qty}张 @ {price} (保证金 {amount_usdt}U × {LEVERAGE}x {MARGIN_MODE}) orderId={order_id}")
                    print(f"   🛡️ 止损 @ {sl_price:.6f}")

                    opened.append({
                        "symbol": ccxt_sym,
                        "side": cand["side"],
                        "qty": qty,
                        "price": price,
                        "amount_usdt": amount_usdt,
                        "rate": cand["rate"],
                        "order_id": order_id,
                    })

                    # ── 回读校验：确认开出来的仓确实是 MARGIN_MODE（全仓/逐仓） ──
                    try:
                        time.sleep(1.0)
                        chk = ex.fetch_positions([ccxt_sym])
                        for cp in chk:
                            if cp.get("contracts") and float(cp["contracts"]) > 0:
                                actual_mm = cp.get("marginMode") or "unknown"
                                if actual_mm != MARGIN_MODE:
                                    print(f"   ⚠️ {ccxt_sym} 仓位模式={actual_mm}，期望={MARGIN_MODE}！")
                                    tg_send(f"⚠️ {ccxt_sym} 开仓后为{actual_mm}，非{MARGIN_MODE}！请检查")
                                else:
                                    print(f"   ✅ {ccxt_sym} 仓位模式已确认={actual_mm}")
                                break
                    except Exception as e:
                        print(f"   (回读校验跳过: {str(e)[:60]})")
                else:
                    print(f"   ❌ {ccxt_sym} 下单失败: {order}")

            except Exception as e:
                print(f"   ❌ {ccxt_sym} 开仓异常: {e}")

        # 日志
        log_entry = {
            "ts": int(time.time() * 1000),
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "candidates_count": len(candidates),
            "opened": opened,
            "skipped": skipped,
        }
        os.makedirs(LOG_DIR, exist_ok=True)
        log_file = os.path.join(LOG_DIR, f"open_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
        with open(log_file, "w") as f:
            json.dump(log_entry, f, indent=2)

        # TG 通知
        if opened:
            msg = f"🚀 开仓 {len(opened)} 个:\n"
            for o in opened:
                msg += f"• {o['symbol']} {o['side']} {o['qty']:.4f}张 @ {o['price']:.4f}\n"
            msg += f"  (保证金 {SINGLE_AMOUNT}U × {LEVERAGE}x {MARGIN_MODE})"
            tg_send(msg)
        if skipped:
            tg_send(f"⏭ 跳过已有持仓: {', '.join(skipped[:5])}{'…' if len(skipped)>5 else ''}")

        # 删除候选文件
        os.remove(CANDIDATES_FILE)
        print(f"[OPEN] ✅ 完成，删除候选文件，共开 {len(opened)} 笔")

    except Exception as e:
        print(f"[OPEN ERROR] {e}")
        import traceback; traceback.print_exc()
        tg_send(f"❌ 开仓异常: {e}")

# ─── 主入口 ────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Bitget 资金费率反弹策略")
    parser.add_argument("action", choices=["scan", "open"], help="scan=扫候选, open=执行开仓")
    parser.add_argument("--wait-until-second", type=int, default=None, help="等待到指定秒数再执行")
    parser.add_argument("--auto-open", action="store_true", help="scan 完成后等待整点自动执行 open")
    args = parser.parse_args()

    if args.action == "scan":
        wait_sec = args.wait_until_second if args.wait_until_second is not None else 55
        cmd_scan(wait_second=wait_sec)
        if args.auto_open:
            # 直接在脚本内等整点，省去 crontab sleep+第二个进程启动开销
            # 给 1s 余量：候选文件已就绪，等 2 秒进下一分钟整点
            now = datetime.now()
            target = now.replace(second=2, microsecond=0) + timedelta(minutes=1)
            delta = (target - datetime.now()).total_seconds()
            if 0 < delta <= 70:
                print(f"[AUTO-OPEN] 等待 {delta:.1f}s 到 {target.strftime('%H:%M:%S')} 执行开仓…")
                time.sleep(delta)
            cmd_open(wait_second=None)
    elif args.action == "open":
        cmd_open(wait_second=None)
