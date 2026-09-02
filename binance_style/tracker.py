"""Bitget 币安式策略 - tracker 跟踪止盈巡检(cron 每分钟拉起)
内部 3 秒拉一次持仓做移动止盈判断; 循环约55秒退出, 等下分钟 cron 再拉起.
"""
import os, sys, time
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
from strategy import _trailing_tp_check, get_exchange, load_opens
from config import TRACKER_POLL_SECONDS

RUN_SECONDS = 55

def main():
    try:
        ex = get_exchange()
        start = time.time()
        while time.time() - start < RUN_SECONDS:
            iter_start = time.time()
            opens = load_opens()
            _trailing_tp_check(ex)
            print(f'[TRACKER-BG] {time.strftime("%H:%M:%S")} 巡检完成, opens持仓={len(opens)}')
            elapsed = time.time() - iter_start
            remain = TRACKER_POLL_SECONDS - elapsed
            if remain > 0:
                time.sleep(remain)
    except Exception as e:
        print(f'[TRACKER-BG] 异常: {str(e)[:150]}')

if __name__ == '__main__':
    main()
