"""Connectivity and privilege check for the RDS instance.

Runs on the ECS (which is inside the same VPC), so a failure here means the
network path — not the credentials — is wrong, and the message says which.

Usage:  /opt/copilot/.venv/bin/python scripts/check_db.py
"""
from __future__ import annotations

import socket
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:  # pragma: no cover
    pass

ENV = Path("/opt/copilot/.env")


def load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        # Strip matching quotes, exactly like app/config.py does. Without this a
        # quoted secret is sent to the server including its quotes, which fails
        # as a *wrong password* and sends you hunting in the wrong place.
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
            v = v[1:-1]
        env[k.strip()] = v
    return env


def main() -> int:
    cfg = load_env(ENV)
    host = cfg.get("DB_HOST", "")
    port = int(cfg.get("DB_PORT", "3306"))
    user = cfg.get("DB_USER", "")
    password = cfg.get("DB_PASSWORD", "")
    name = cfg.get("DB_NAME", "copilot")

    print(f"配置文件 : {ENV}")
    print(f"目标     : {user}@{host or '<未填>'}:{port}/{name}")
    print()

    if not host:
        print("✗ DB_HOST 为空，请先在 .env 中填写 RDS 内网地址")
        return 2

    print("== 1/3 TCP 连通性 ==")
    try:
        sock = socket.create_connection((host, port), timeout=6)
        sock.close()
        print(f"  ✓ {host}:{port} 可连接")
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ {host}:{port} 不可达: {type(exc).__name__}: {exc}")
        print()
        print("  排查顺序：")
        print("   1. RDS 白名单是否包含本机私网 IP 172.24.19.160")
        print("   2. RDS 专有网络是否与 ECS 相同（vpc-bp13ztthwqmezpvbv72kl）")
        print("   3. 是否误用了公网地址（应使用 *internal* 内网地址）")
        return 1

    print("== 2/3 认证 ==")
    try:
        import pymysql

        conn = pymysql.connect(
            host=host, port=port, user=user, password=password, database=name,
            charset="utf8mb4", connect_timeout=8,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ 认证失败: {type(exc).__name__}: {exc}")
        print("  检查 DB_USER / DB_PASSWORD，以及账号是否有该库的权限")
        return 1

    print("  ✓ 认证通过")
    print("== 3/3 权限与容量 ==")
    try:
        cur = conn.cursor()
        cur.execute("SELECT VERSION()")
        print(f"  版本   : {cur.fetchone()[0]}")
        cur.execute("SELECT DATABASE()")
        print(f"  当前库 : {cur.fetchone()[0]}")
        cur.execute("SHOW TABLES")
        tables = [r[0] for r in cur.fetchall()]
        print(f"  已有表 : {tables if tables else '（空库）'}")
        # create/drop probe proves DDL rights without leaving anything behind
        cur.execute("CREATE TABLE IF NOT EXISTS _perm_probe (id INT)")
        cur.execute("DROP TABLE _perm_probe")
        print("  ✓ 具备建表权限（迁移需要）")
        cur.execute("SELECT @@character_set_database, @@collation_database")
        charset, collation = cur.fetchone()
        print(f"  字符集 : {charset} / {collation}")
    finally:
        conn.close()

    print()
    print("✓ 全部检查通过，可以执行迁移：")
    print("   /opt/copilot/.venv/bin/python scripts/migrate_to_mysql.py --dry-run")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
