#!/usr/bin/env python3
"""
backup.py — 每套账户独立打包备份

将 funding_rebound 策略代码 + 该账户 .env(密钥) 打包成本地 tar.gz，
按账户ID独立分目录存放。密钥仅本地，不进 git 仓库。

用法:
    python3 backup.py                   # 打包当前账户
    python3 backup.py push              # 打包并 push 代码到 git 仓库(不含密钥)
    python3 backup.py list              # 列出已有备份包
"""
import sys, os, json, subprocess, time, re, tarfile

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
sys.path.insert(0, HERE)

BACKUP_ROOT = os.path.join(os.path.dirname(HERE), "backups")

CODE_FILES = [
    "config.py", "crontab.conf", "funding_crop.py",
    "load_env.py", "tracker.py", "backup.py", ".gitignore",
]


def account_id():
    try:
        import load_env
        from config import API_KEY
        ak = API_KEY or ""
    except Exception:
        ak = ""
    aid = f"acct_{'x'.join([ak[i:i+6] for i in range(0, min(len(ak), 12), 6)])}"
    return re.sub(r'[^A-Za-z0-9]', '_', aid)[:30]


def build_package():
    aid = account_id()
    ts = time.strftime("%Y%m%d_%H%M%S")
    acct_dir = os.path.join(BACKUP_ROOT, aid)
    os.makedirs(acct_dir, exist_ok=True)
    pkg = os.path.join(acct_dir, f"{aid}_{ts}.tar.gz")

    files = [f for f in CODE_FILES if os.path.exists(f)]
    if os.path.exists(".env"):
        files.append(".env")  # 密钥仅进本地包

    with tarfile.open(pkg, "w:gz") as tar:
        for f in files:
            tar.add(f, arcname=os.path.join("funding_rebound", f))

    head = subprocess.run(["git", "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    manifest = {
        "account_id": aid,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "package": os.path.basename(pkg),
        "contains_secrets": ".env" in files,
        "git_commit": head,
        "files": files,
    }
    mf = os.path.join(acct_dir, f"{aid}_{ts}.manifest.json")
    with open(mf, "w") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)

    print(f"[BACKUP] 账户  {aid}")
    print(f"[BACKUP] 包     {pkg} ({os.path.getsize(pkg)} bytes)")
    print(f"[BACKUP] 清单   {mf}")
    print(f"[BACKUP] git    {head[:8]}")
    return pkg


def git_push():
    # 代码入库(不含 .env 密钥, 已 gitignore)
    subprocess.run(["git", "add", "-A"], check=False)
    subprocess.run(
        ["git", "commit", "-m", f"backup auto {time.strftime('%Y-%m-%d %H:%M:%S')}"],
        capture_output=True)
    r = subprocess.run(["git", "push"], capture_output=True, text=True)
    print("[GIT]", r.stdout.strip() or r.stderr.strip())


def list_pkgs():
    if not os.path.isdir(BACKUP_ROOT):
        print("[BACKUP] 无备份目录")
        return
    for d in sorted(os.listdir(BACKUP_ROOT)):
        dd = os.path.join(BACKUP_ROOT, d)
        if os.path.isdir(dd):
            pkgs = [x for x in os.listdir(dd) if x.endswith(".tar.gz")]
            if pkgs:
                print(f"[{d}] {len(pkgs)} 个备份包")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        if sys.argv[1] == "push":
            build_package(); git_push()
        elif sys.argv[1] == "list":
            list_pkgs()
        else:
            print("未知参数")
    else:
        build_package()
