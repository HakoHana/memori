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
    nohup python -m memori --port $PORT > /dev/null 2>&1 &
    disown
    # 等待就绪（最多等 15 秒）
    for i in $(seq 1 15); do
        if curl -sf http://localhost:$PORT/health > /dev/null 2>&1; then
            echo "✅ memori 已就绪"
            break
        fi
        if [ $i -eq 15 ]; then
            echo "❌ memori 启动超时，请检查日志"
            exit 1
        fi
        sleep 1
    done
fi

# 打开配置页面
xdg-open "http://localhost:$PORT/config"
echo "🌐 配置页面已打开"
