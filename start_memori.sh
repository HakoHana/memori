#!/usr/bin/env bash
# 启动 Memori Dashboard + API 服务
# 如果与 AstrBot 配合使用，设置数据目录指向 AstrBot 的插件数据目录
export MEMORIA_DATA_DIR="${MEMORIA_DATA_DIR:-$HOME/data/plugin_data/memori}"
exec python -m memori --port 8765
