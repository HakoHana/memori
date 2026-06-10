#!/bin/bash
# memori 配置面板 — 一键启动 + 打开浏览器

PORT=8765
MEMORI_DIR="/home/hako/桌面/claude/memori"

# 检查 memori 是否已在运行
if curl -sf http://localhost:$PORT/health > /dev/null 2>&1; then
    echo "✅ memori 已在运行"
else
    echo "🚀 启动 memori..."
    cd "$MEMORI_DIR"
    python -m memori --port $PORT &
    # 等待就绪（最多等 10 秒）
    for i in $(seq 1 10); do
        if curl -sf http://localhost:$PORT/health > /dev/null 2>&1; then
            break
        fi
        sleep 1
    done
fi

# 打开配置页面
xdg-open "http://localhost:$PORT/config"
echo "🌐 配置页面已打开"
