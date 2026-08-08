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


# ─── 阶段一:扫描 ──────────────────────────────────────────────────
def cmd_scan(wait_second=40):
    print(f"[SCAN] 开始扫描,等待到 {wait_second} 秒...")
    wait_until_second(wait_second)

    try:
        ex = get_exchange()
        # 1. 批量获取所有 ticker(含费率)
        tickers_raw = bitget_v2_get("/api/v2/mix/market/tickers?productType=USDT-FUTURES")
        if tickers_raw.get("code") != "00000":
            raise Exception(f"tickers API 失败: {tickers_raw.get('msg')}")

        # 2. 批量获取所有合约配置(含 fundInterval)
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

        # 带单限制币黑名单(自学习:开仓被拒过的币自动跳过)
        copy_black = load_copy_blacklist()
        if copy_black:
            print(f"[SCAN] 带单限制黑名单 {len(copy_black)} 个: {sorted(copy_black)}")

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

            # 带单限制币:跳过
            base = sym[:-4] if sym.upper().endswith("USDT") else sym
            if base.upper() in copy_black:
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

            # 筛选:距结算 < 60s
            if countdown_ms > SETTLE_WINDOW * 1000 or countdown_ms < -2000:
                continue

            # 开仓方向：正费率→做空(收资金费)  负费率→做多(收资金费)
            side = "short" if rate > 0 else "long"
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
            print(f"[SCAN] ✅ {len(candidates)} 个候选,已写入 {CANDIDATES_FILE}")
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

# ─── 阶段二:开仓 ──────────────────────────────────────────────────
def cmd_open(wait_second=None):
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

    print(f"[OPEN] 候选 {len(candidates)} 个,开始开仓...")

    try:
        ex = get_exchange()

        # 设置账户为单向持仓模式
        try:
            ex.set_position_mode(hedged=False)
            print(f"[OPEN] ✅ 已设置单向持仓模式")
        except Exception as e:
            print(f"[OPEN] ⚠️ 设置持仓模式(可忽略): {e}")

        # 先拉一次当前持仓(symbol 列表)
        existing_positions = set()
        try:
            positions = ex.fetch_positions()
            for p in positions:
                if p.get("contracts") and float(p["contracts"]) > 0:
                    existing_positions.add(p["symbol"])
        except Exception as e:
            print(f"[OPEN] 拉持仓失败(继续): {e}")

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
                    print(f"   ⏭ {sym_raw} 排除币种,跳过")
                    skip_ex = True
                    break
            if skip_ex:
                continue

            # 检查已有持仓(同币种去重)
            if ccxt_sym in existing_positions:
                print(f"   ⏭ {ccxt_sym} 已有持仓,跳过")
                skipped.append(ccxt_sym)
                continue

            # 实时检查同币种是否已有未平仓位(防止数据延迟导致重复开仓)
            try:
                live_pos = ex.fetch_positions([ccxt_sym])
                for lp in live_pos:
                    if lp.get("contracts") and float(lp["contracts"]) > 0:
                        print(f"   ⏭ {ccxt_sym} 实时检查有持仓,跳过")
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
                # ── 先设置保证金模式(必须在该币无持仓/无挂单时才能切换成功)──
                # 注意:经典账户切换保证金模式会重置该币杠杆为默认值,
                # 所以必须先切模式,再设杠杆(顺序不能反,否则5x会被重置回默认)。
                try:
                    ex.set_margin_mode(MARGIN_MODE, ccxt_sym)
                except Exception as e:
                    print(f"   ⚠️ {ccxt_sym} set_margin_mode 失败(继续,后续严格验证杠杆): {str(e)[:80]}")

                # ── 再设置杠杆,并回读确认真的设成了 LEVERAGE x(不盲信成功)──
                leverage_ok = False
                for lev_retry in range(3):
                    try:
                        lev_resp = ex.set_leverage(LEVERAGE, ccxt_sym)
                        # 回读 set_leverage 返回里实际生效的杠杆值(经典账户返回 dict)
                        lev_data = (lev_resp or {}).get("data") or {}
                        if isinstance(lev_data, dict):
                            long_l = lev_data.get("longLeverage")
                            short_l = lev_data.get("shortLeverage")
                            got = {(long_l), (short_l)}
                        else:
                            # UTA 返回 data 为字符串,无法校验,按成功处理
                            got = {str(LEVERAGE)}
                        if got == {str(LEVERAGE)}:
                            leverage_ok = True
                            print(f"   ✅ {ccxt_sym} 杠杆已确认 {LEVERAGE}x (long={long_l} short={short_l})")
                            break
                        else:
                            print(f"   ⚠️ {ccxt_sym} 杠杆设置后回读为 {long_l}/{short_l}x(期望{LEVERAGE}x),重试 ({lev_retry+1}/3)")
                    except Exception as e:
                        err_str = str(e)
                        print(f"   ⚠️ {ccxt_sym} 设置杠杆异常: {err_str[:60]},重试 ({lev_retry+1}/3)")
                    time.sleep(0.5)

                if not leverage_ok:
                    print(f"   ❌ {ccxt_sym} 杠杆设置失败/未生效(期望 {LEVERAGE}x),跳过此币种")
                    tg_send(f"⚠️ {ccxt_sym} 开仓跳过:杠杆未确认,期望 {LEVERAGE}x")
                    continue

                # 获取当前市价,计算数量
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

                # ── 下单(先不带止损;止损放到"确认全仓"之后,避免给非全仓仓挂止损) ──
                sl_price = price * 0.2 if cand["side"] == "long" else price * 1.8
                order = ex.create_order(ccxt_sym, "market", side, float(qty), None, {
                    "marginMode": MARGIN_MODE,
                    "productType": "USDT-FUTURES",
                })
                if order and order.get("id"):
                    order_id = order["id"]
                    print(f"   ✅ {ccxt_sym} {side} {qty}张 @ {price} (保证金 {amount_usdt}U × {LEVERAGE}x {MARGIN_MODE}) orderId={order_id}")

                    opened.append({
                        "symbol": ccxt_sym,
                        "side": cand["side"],
                        "qty": qty,
                        "price": price,
                        "amount_usdt": amount_usdt,
                        "rate": cand["rate"],
                        "order_id": order_id,
                        "margin_mode": MARGIN_MODE,
                        "margin_mode_confirmed": False,
                    })

                    # ── 第1步:强制补切全仓(关键:Bitget 下单不认 marginMode 参数,
                    #    新开仓默认/保持 isolated,必须下单后显式 set_margin_mode 切回 crossed) ──
                    try:
                        time.sleep(1.0)
                        try:
                            set_r = ex.set_margin_mode(MARGIN_MODE, ccxt_sym)
                            data_mm = (set_r.get("data") or {}).get("marginMode") if isinstance(set_r, dict) else None
                            print(f"   🔄 {ccxt_sym} 补切全仓返回: {data_mm}")
                        except Exception as se:
                            print(f"   ⚠️ {ccxt_sym} 补切全仓失败,尝试回读: {str(se)[:80]}")
                            time.sleep(1.0)
                    except Exception as e:
                        print(f"   (补切异常: {str(e)[:60]})")

                    # ── 第2步:回读确认最终仓位模式 ──
                    # 注意:新开仓后交易所撮合/仓位同步有延迟,单次 fetch_positions 可能读不到该仓
                    # (返回空→被误判 unverified→跳过止损,造成仓位裸奔无止损)。
                    # 故这里必须重试多次,直到确认仓位的 marginMode 再决定是否挂止损。
                    actual_mm = "unverified"
                    margin_ok = False
                    try:
                        for mm_retry in range(6):
                            chk = ex.fetch_positions([ccxt_sym])
                            for cp in chk:
                                if cp.get("contracts") and float(cp["contracts"]) > 0:
                                    actual_mm = cp.get("marginMode") or "unknown"
                                    # Bitget/ccxt 可能返回 cross 或 crossed,都归一化为 crossed 比较
                                    norm_actual = "crossed" if str(actual_mm).lower() in ("cross", "crossed") else str(actual_mm).lower()
                                    margin_ok = (norm_actual == MARGIN_MODE)
                                    opened[-1]["actual_margin_mode"] = actual_mm
                                    if margin_ok:
                                        print(f"   ✅ {ccxt_sym} 仓位模式已确认={actual_mm} (全仓) ")
                                        opened[-1]["margin_mode_confirmed"] = True
                                    else:
                                        print(f"   ⚠️ {ccxt_sym} 仓位模式={actual_mm},期望={MARGIN_MODE}!")
                                    break
                            if margin_ok or len(chk) > 0:
                                break  # 已确认到该仓(无论是否全仓),不必再等
                            # 该仓还没出现在持仓里(可能是同步延迟),等一会重试
                            print(f"   ⏳ {ccxt_sym} 回读暂未读到该仓,等待重试({mm_retry+1}/6)...")
                            time.sleep(1.0)
                    except Exception as e:
                        print(f"   (回读校验异常: {str(e)[:60]})")

                    # ── 第3步：只有确认是全仓才设置止损；非全仓则不设止损并告警 ──
                    if margin_ok:
                        # ── 3.0 关键时刻：补切全仓的 set_margin_mode 会重置杠杆为默认值（常见10x），
                        #    必须重新确认/补设杠杆为 LEVERAGE x，否则开出来是默认杠杆而非5x ──
                        try:
                            # 先回读当前实际杠杆
                            chk_pos = [p for p in ex.fetch_positions([ccxt_sym]) if p.get("contracts") and float(p["contracts"])>0]
                            if chk_pos:
                                cur_lev = float(chk_pos[0].get("leverage") or 0)
                                if abs(cur_lev - LEVERAGE) > 1e-6:
                                    print(f"   🔧 {ccxt_sym} 开仓后杠杆={cur_lev}x≠{LEVERAGE}x，重新补设 {LEVERAGE}x")
                                    for lr in range(3):
                                        try:
                                            lr2 = ex.set_leverage(LEVERAGE, ccxt_sym)
                                            ld2 = (lr2 or {}).get("data") or {}
                                            lv = str(ld2.get("crossMarginLeverage", ld2.get("longLeverage", ""))) if isinstance(ld2, dict) else str(LEVERAGE)
                                            if str(LEVERAGE) in str(lv):
                                                print(f"   ✅ {ccxt_sym} 杠杆已补设为 {LEVERAGE}x")
                                                break
                                        except Exception as ee:
                                            print(f"   ⚠️ {ccxt_sym} 补设杠杆异常: {str(ee)[:50]}")
                                        time.sleep(0.5)
                        except Exception as e:
                            print(f"   ⚠️ {ccxt_sym} 检查/补设杠杆异常: {str(e)[:60]}")

                        # ── 止损挂单（重要）：必须用 create_trigger_order 挂"计划单 reduceOnly"，
                        #    不要用 create_order(stopLoss=...) -- 后者走 PlaceTpslOrder 端点,
                        #    对新开仓会报 22002 "No position to close",导致止损永远挂不上。
                        #    正确做法:反向 reduce 单(多仓 sell / 空仓 buy)+ triggerPrice,
                        #    触发价按合约 pricePlace 精度 floor 舍入,否则报 checkBDScale error。
                        try:
                            reduce_side = "sell" if cand["side"] == "long" else "buy"   # 平仓方向(反向):多仓→sell 空仓→buy
                            pp = int((market.get("info") or {}).get("pricePlace", "6"))
                            sl_price_r = math.floor(sl_price * (10 ** pp)) / (10 ** pp)
                            sl_order = ex.create_trigger_order(
                                ccxt_sym, "market", reduce_side, float(qty), None, sl_price_r, {
                                    "marginMode": MARGIN_MODE,
                                    "productType": "USDT-FUTURES",
                                    "reduceOnly": True,
                                }
                            )
                            sl_ok = bool(sl_order and sl_order.get("id"))
                            # 回读确认止损计划单真的挂上了
                            sl_verified = False
                            try:
                                ex.load_markets()
                                sid = (ex.market(ccxt_sym) or {}).get("id")
                                pr = ex.private_mix_get_v2_mix_order_orders_plan_pending({
                                    "productType": "USDT-FUTURES", "symbol": sid, "planType": "normal_plan"
                                })
                                plan_list = (pr.get("data") or {}).get("entrustedList", [])
                                sl_verified = any(
                                    str(x.get("side")) == reduce_side
                                    and abs(float(x.get("triggerPrice", 0)) - sl_price_r) < 1e-8
                                    for x in plan_list
                                )
                            except Exception as ve:
                                print(f"   (止损回读校验异常: {str(ve)[:60]})")
                            if sl_ok and sl_verified:
                                print(f"   🛡️ {ccxt_sym} 止损已设置并确认 @ {sl_price_r} (反向reduce下单,仓位全仓)")
                            elif sl_ok:
                                print(f"   🛡️ {ccxt_sym} 止损已下单 @ {sl_price_r},但回读未确认(需人工核) ✓")
                            else:
                                print(f"   ⚠️ {ccxt_sym} 止损下单返回异常: {sl_order}")
                        except Exception as se:
                            print(f"   ⚠️ {ccxt_sym} 设置止损失败: {str(se)[:100]}")

                        # ── 止盈挂单（20%）：跟止损一样用 create_trigger_order 挂 reduceOnly 计划单 ──
                        #    多仓 TP=开仓x1.2(+20%)→sell 平  空仓 TP=开仓x0.8(-20%)→buy 平
                        #    方向与止损相同(reduce_side 反向平仓),仅触发价不同。
                        try:
                            tp_price = price * 1.2 if cand["side"] == "long" else price * 0.8
                            tp_price_r = math.floor(tp_price * (10 ** pp)) / (10 ** pp)
                            tp_order = ex.create_trigger_order(
                                ccxt_sym, "market", reduce_side, float(qty), None, tp_price_r, {
                                    "marginMode": MARGIN_MODE,
                                    "productType": "USDT-FUTURES",
                                    "reduceOnly": True,
                                }
                            )
                            tp_ok = bool(tp_order and tp_order.get("id"))
                            tp_verified = False
                            try:
                                ex.load_markets()
                                sid = (ex.market(ccxt_sym) or {}).get("id")
                                pr = ex.private_mix_get_v2_mix_order_orders_plan_pending({
                                    "productType": "USDT-FUTURES", "symbol": sid, "planType": "normal_plan"
                                })
                                plan_list = (pr.get("data") or {}).get("entrustedList", [])
                                tp_verified = any(
                                    str(x.get("side")) == reduce_side
                                    and abs(float(x.get("triggerPrice", 0)) - tp_price_r) < 1e-8
                                    for x in plan_list
                                )
                            except Exception as ve:
                                print(f"   (止盈回读校验异常: {str(ve)[:60]})")
                            if tp_ok and tp_verified:
                                print(f"   🎯 {ccxt_sym} 止盈已设置并确认 @ {tp_price_r} (+20%)")
                            elif tp_ok:
                                print(f"   🎯 {ccxt_sym} 止盈已下单 @ {tp_price_r},但回读未确认(需人工核) ✓")
                            else:
                                print(f"   ⚠️ {ccxt_sym} 止盈下单返回异常: {tp_order}")
                        except Exception as te:
                            print(f"   ⚠️ {ccxt_sym} 设置止盈失败: {str(te)[:100]}")
                    else:
                        print(f"   ❌ {ccxt_sym} 非全仓({actual_mm}),跳过了止损设置!")
                        tg_send(f"❌ {ccxt_sym} 仓位为{actual_mm}(非全仓),未设置止损,请人工处理")
                else:
                    print(f"   ❌ {ccxt_sym} 下单失败: {order}")

            except Exception as e:
                print(f"   ❌ {ccxt_sym} 开仓异常: {e}")
                # 带单限制币:自动加入黑名单,后续扫描跳过(避免每整点白跑)
                if is_copy_trade_error(e):
                    base_b = base.upper()
                    black = load_copy_blacklist()
                    if base_b not in black:
                        black.add(base_b)
                        save_copy_blacklist(black)
                        print(f"   🚫 {ccxt_sym} 带单限制币,已加入黑名单: {base_b}")
                        tg_send(f"🚫 {ccxt_sym} 带单限制币,已自动加入跳过黑名单")
                else:
                    tg_send(f"⚠️ {ccxt_sym} 开仓异常: {str(e)[:80]}")

        # 日志
        log_entry = {
            "ts": int(time.time() * 1000),
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "config": {
                "margin_mode": MARGIN_MODE,
                "leverage": LEVERAGE,
                "amount_usdt": SINGLE_AMOUNT,
            },
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
            msg = f"🚀 开仓 {len(opened)} 个 ({MARGIN_MODE}, {LEVERAGE}x):\n"
            for o in opened:
                mm_tag = o.get("margin_mode_confirmed")
                tag = "✅全仓" if (mm_tag and MARGIN_MODE=="crossed") else ("⚠️" + str(o.get("actual_margin_mode","?")))
                msg += f"• {o['symbol']} {o['side']} {o['qty']:.4f}张 @ {o['price']:.6f} [{tag}]\n"
            msg += f"  (保证金 {SINGLE_AMOUNT}U × {LEVERAGE}x {MARGIN_MODE})"
            tg_send(msg)
        if skipped:
            tg_send(f"⏭ 跳过已有持仓: {', '.join(skipped[:5])}{'...' if len(skipped)>5 else ''}")

        # 删除候选文件
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
