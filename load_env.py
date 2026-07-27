"""在 funding_crop.py tracker.py 导入 config 前先加载 .env"""
import os, sys
dotenv_path = os.path.join(os.path.dirname(__file__), '.env')
if os.path.exists(dotenv_path):
    with open(dotenv_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            k, _, v = line.partition('=')
            os.environ.setdefault(k.strip(), v.strip())
