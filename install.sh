#!/usr/bin/env bash
set -euo pipefail

# ============================================================
#  hermes-kook-adapter 一键安装脚本
#  https://github.com/WOO-MX/hermes-kook-adapter
# ============================================================

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

say() { printf "${GREEN}==>${NC} %s\n" "$*"; }
warn() { printf "${YELLOW}⚠${NC}  %s\n" "$*"; }
err() { printf "${RED}✗${NC}  %s\n" "$*" >&2; }
info() { printf "    %s\n" "$*"; }

echo ""
echo "  ╔══════════════════════════════════════════╗"
echo "  ║   Hermes KOOK 适配器 — 一键安装          ║"
echo "  ╚══════════════════════════════════════════╝"
echo ""

# ── 1. 定位 Hermes 数据目录 ──────────────────────────────
say "查找 Hermes 数据目录..."

HERMES_HOME="${HERMES_HOME:-}"
if [ -z "$HERMES_HOME" ]; then
    if command -v hermes &>/dev/null; then
        HERMES_HOME=$(hermes config path 2>/dev/null | xargs dirname || true)
    fi
fi
if [ -z "$HERMES_HOME" ]; then
    for guess in "$HOME/.hermes" "/opt/data" "$HOME"; do
        if [ -f "$guess/config.yaml" ] || [ -d "$guess/plugins" ]; then
            HERMES_HOME="$guess"
            break
        fi
    done
fi

PLUGIN_DIR="${HERMES_HOME:-$HOME/.hermes}/plugins/platforms/kook"
say "安装目标: $PLUGIN_DIR"

# ── 2. 获取源码 ──────────────────────────────────────────
SOURCE_DIR="${SOURCE_DIR:-$(cd "$(dirname "$0")" && pwd)}"

if [ "$SOURCE_DIR" = "/tmp" ] || [ ! -f "$SOURCE_DIR/adapter.py" ]; then
    # 远程安装：从 GitHub 下载
    say "从 GitHub 下载源码..."
    TMPDIR=$(mktemp -d)
    trap "rm -rf $TMPDIR" EXIT

    if command -v git &>/dev/null; then
        git clone --depth 1 https://github.com/WOO-MX/hermes-kook-adapter.git "$TMPDIR" 2>&1 | sed 's/^/    /'
        SOURCE_DIR="$TMPDIR"
    elif command -v curl &>/dev/null; then
        # GitHub tarball fallback（无墙可直接访问）
        curl -fsSL https://github.com/WOO-MX/hermes-kook-adapter/archive/refs/heads/main.tar.gz | tar xz -C "$TMPDIR" --strip-components=1
        SOURCE_DIR="$TMPDIR"
    else
        err "需要 git 或 curl，请先安装"
        exit 1
    fi
fi

# ── 3. 安装依赖 ──────────────────────────────────────────
say "安装 Python 依赖..."

if command -v uv &>/dev/null; then
    uv pip install --system aiohttp httpx 2>&1 | sed 's/^/    /'
elif command -v pip3 &>/dev/null; then
    pip3 install aiohttp httpx 2>&1 | sed 's/^/    /'
elif command -v pip &>/dev/null; then
    pip install aiohttp httpx 2>&1 | sed 's/^/    /'
else
    warn "未检测到 pip，跳过依赖安装（请手动安装 aiohttp 和 httpx）"
fi

# ── 4. 部署插件 ──────────────────────────────────────────
say "部署插件文件..."

mkdir -p "$PLUGIN_DIR"
for f in adapter.py __init__.py plugin.yaml; do
    if [ -f "$SOURCE_DIR/$f" ]; then
        cp -v "$SOURCE_DIR/$f" "$PLUGIN_DIR/" | sed 's/^/    /'
    else
        warn "缺少文件: $f"
    fi
done

# 清理 __pycache__
rm -rf "$PLUGIN_DIR/__pycache__" 2>/dev/null || true

# ── 5. 配置检查 ──────────────────────────────────────────
say "检查配置..."

if [ -z "${KOOK_TOKEN:-}" ]; then
    echo ""
    warn "KOOK_TOKEN 未设置。请配置以下环境变量："
    echo ""
    echo "  ${CYAN}必填：${NC}"
    echo "    export KOOK_TOKEN=\"Bot_xxxxxxxxxxxxxxxx\""
    echo ""
    echo "  ${CYAN}可选：${NC}"
    echo "    export KOOK_HOME_CHANNEL=\"频道ID\"          # cron 投递目标"
    echo "    export KOOK_ALLOWED_USERS=\"用户ID1,用户ID2\"  # 白名单"
    echo "    export KOOK_ALLOW_ALL_USERS=true              # 开发模式"
    echo ""
    echo "  或写入 ~/.hermes/.env："
    echo "    echo 'KOOK_TOKEN=Bot_xxx' >> ~/.hermes/.env"
    echo ""
else
    info "KOOK_TOKEN 已设置 ✓"
fi

# ── 6. 重载插件 ──────────────────────────────────────────
say "重载插件..."

if command -v hermes &>/dev/null; then
    if hermes plugins list 2>/dev/null | grep -q "kook"; then
        say "KOOK 插件已注册，请在 Hermes 中运行 /reload 或重启 gateway"
    else
        warn "未检测到已注册的 KOOK 插件"
        info "如果已通过 config.yaml 启用，重启 gateway 即可加载"
    fi
else
    warn "未检测到 hermes CLI，跳过重载"
    info "请手动重启 Hermes Gateway：hermes gateway restart"
fi

# ── 完成 ──────────────────────────────────────────────────
echo ""
echo "  ┌─────────────────────────────────────────┐"
echo "  │  ${GREEN}✓ 安装完成${NC}                               │"
echo "  ├─────────────────────────────────────────┤"
echo "  │  插件目录: $PLUGIN_DIR"
echo "  │  源码仓库: https://github.com/WOO-MX/hermes-kook-adapter"
echo "  ├─────────────────────────────────────────┤"
echo "  │  获取 Bot Token: https://developer.kookapp.cn/app/index"
echo "  │  重启网关: hermes gateway restart         │"
echo "  └─────────────────────────────────────────┘"
echo ""
