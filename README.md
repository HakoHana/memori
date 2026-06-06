# Memory  AstrBot 日记式长期记忆插件

让 Bot 记住与用户的每一刻，用日记的方式沉淀记忆。

## 功能

-  **日记式记忆**  LLM 以第一人称写日记，记录对话中的重要 moment
-  **原子事实**  从日记提取结构化事实，支持 FTS5 全文搜索
-  **知识图谱**  自动构建实体关联图，可视化浏览
-  **用户画像**  长期沉淀的用户特征画像
-  **WebUI Dashboard**  知识图谱可视化、记忆管理、日记编辑

## 安装

将插件目录放入 `data/plugins/`，在 AstrBot WebUI 中重载插件。

## 配置

在 WebUI 插件配置页可配置：
- 整理频率（按消息数/按时间）
- LLM 模型选择
- 记忆注入位置
- 召回参数等

## 数据位置

`data/plugin_data/astrbot_plugin_memory/memory.db`
