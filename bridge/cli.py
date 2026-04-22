#!/usr/bin/env python3
"""用户管理命令行工具 — python3 -m bridge.cli user list/create/delete"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bridge import users

def cmd_list():
    rows = users.list_users()
    print(f"{'ID':<4} {'用户名':<20} {'角色':<8} {'显示名':<20} {'启用':<6} {'最后登录':<20}")
    print("-" * 80)
    for u in rows:
        from datetime import datetime
        t = datetime.fromtimestamp(u["last_login"]).strftime("%Y-%m-%d %H:%M") if u["last_login"] else "从未"
        print(f"{u['id']:<4} {u['username']:<20} {u['role']:<8} {u['display_name']:<20} {'是' if u['enabled'] else '否':<6} {t:<20}")

def cmd_create(username, password, role="user", display_name=""):
    u = users.create_user(username, password, role, display_name or username)
    if u:
        print(f"✅ 用户创建成功: {username} ({role})")
    else:
        print(f"❌ 用户名 {username} 已存在")

def cmd_delete(uid):
    users.delete_user(int(uid))
    print(f"✅ 用户 ID={uid} 已删除")

if __name__ == "__main__":
    users.bootstrap()
    cmd = sys.argv[1] if len(sys.argv) > 1 else None
    if cmd == "list":
        cmd_list()
    elif cmd == "create" and len(sys.argv) >= 4:
        cmd_create(sys.argv[2], sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else "user", sys.argv[5] if len(sys.argv) > 5 else "")
    elif cmd == "delete" and len(sys.argv) >= 3:
        cmd_delete(sys.argv[2])
    else:
        print("用法:")
        print("  python3 -m bridge.cli list                              # 列出所有用户")
        print("  python3 -m bridge.cli create <用户名> <密码> [角色] [显示名]  # 创建用户")
        print("  python3 -m bridge.cli delete <用户ID>                    # 删除用户")
        print("\n角色: admin / user")
