# 交接文档 (HANDOFF)

## 仓库总览

- **定位**：RGBDT 视觉定位测试集真值（Ground Truth）标注工具（单页 Canvas 界面 + 轻量 HTTP 服务 + 模型自动预打标 worker）。
- **运行环境**：本地 Python 3.12（纯标准库，前端无外部 npm 依赖，离线可用）。
- **核心数据流**：
  `manifest.json`（输入条目清单）→ `server.py` / `web/`（人工标注审核）+ `auto_annotate.py`（GLM-4.6V 自动预标）→ `annotations.jsonl`（追加日志）+ `annotations.predictions.json`（评测/后处理格式快照）。

## 当前状态（2026-09-03）

- **数据进度（Part 1）**：
  - 清单：`data/manifest.json` 为 `queries_part1`，总计 3184 条。
  - 人工核验已完成：332 条（`000002_001` 至 `001140_006`），标注者署名已全量回填为 `fang0`。
  - 待处理：剩余 2852 条，由 `auto_annotate.py` 负责初标，随后人工通过网页复核。
- **服务与前端状态**：
  - Token 鉴权已移除，服务端开放直连，前端无弹窗干扰。
  - 标注徽章与画框区分：
    - 绿色 `已核验 (fang0)`：人工确认或手动绘制的条目。
    - 紫色 `🤖 待审AI预标 (glm-4.6v)`：模型生成的待核验条目，画布框呈紫色并带有 `[AI预标]` 标识。
    - 灰色 `⚠️ 目标不存在 (AI判空)`：模型判定无目标条目。
    - 橙色 `未标注`：完全未处理条目。
  - 导航增强：顶栏新增「上一AI」「下一AI」按钮（快捷键 `Alt + ←` / `Alt + →`）；审核状态下敲击 `Enter` 保存并自动跳转至下一条待审 AI 条目。
- **自动化脚本状态**：
  - `auto_annotate.py`：基于纯标准库的多模态标注 worker。
  - 配置：支持 8 并发，断点续跑；显式禁用思考模式（`thinking: {"type": "disabled"}`）；强制单行 JSON 格式（`response_format: {"type": "json_object"}`）；直接输入英文原版 query；去除 reason 等多余字段。
  - 测试状态：35 项单测全绿（`python3 -m unittest discover tests`）。

## 常用命令

### 1. 运行自动打标（GLM-4.6V 8并发断点续跑）
```bash
export API_KEY=你的智谱Key
python3 auto_annotate.py --manifest data/manifest.json --data-dir data \
    --model glm-4.6v --concurrency 8 -v
```

### 2. 启动前端审查服务
```bash
python3 server.py --manifest data/manifest.json --data-dir data \
    --host 0.0.0.0 --port 8765
```
浏览器打开 `http://localhost:8765/` 进行核验。

### 3. 本地单测验证
```bash
python3 -m unittest discover -s tests
```

## 交接日志

### 2026-09-03（人机协同预标流程落地、鉴权解耦与审核交互升级）

- **动因**：
  1. 人工全量标注 3184 条效率受限，需引入视觉大模型进行批量预标与无目标拒识。
  2. 简化本地标注工具访问流程，移除无必要的 Token 鉴权弹窗。
  3. 历史标注遗漏 annotator 字段，需回填个人署名确立所有权。
  4. 需提供便捷的机器标注与人工标注区分机制及快速跳转审核流。
- **改动**：
  1. **历史回填**：扫描 `data/annotations.jsonl`，将 332 条有效历史标注中的空 annotator 字段全部修正为 `"fang0"`，更新快照。
  2. **鉴权解耦**：移除 `server.py` 中的 `_authorized` 拦截，移除 `web/index.html` 中的 Token 按钮与弹窗，精简 `web/app.js` 中的 Token 状态存储与 Header 注入，更新 `tests/test_server.py` 单测。
  3. **界面视觉与交互升级**：
     - 增加 `badge-ai`（紫色）和 `badge-absent`（灰色）状态徽章。
     - 画布 `drawBbox` 支持 AI 标注特殊渲染（紫色框体 + `🤖 [AI预标]` 浮动标识）。
     - 增加 `goToPrevAi` 与 `goToNextAi`，绑定按钮与 `Alt + ←` / `Alt + →` 快捷键。
     - `saveBbox` 增加智能跳转逻辑：当前方存在待审 AI 条目时，保存后优先跳至下一条 AI 预标条目。
  4. **构建 `auto_annotate.py`**：
     - 对齐 `translate.py` 的轻量无依赖设计，采用 `ThreadPoolExecutor` + 原子落盘 + 日志增量续跑。
     - 增加拒识判断：无目标时写入 `annotator: "glm-4.6v:absent"` 且不产生框。
     - 修复默认思考模式引发的 429 频控问题：显式注入 `"thinking": {"type": "disabled"}`。
     - 去除中英提示词包装与自然语言 reason 输出，直接向模型输入原装英文 query，模型仅输出纯净坐标字典。
- **验证结果**：
  - 单测 `python3 -m unittest discover tests` 35 项全部通过（5.6s）。
  - `auto_annotate.py` 实测 8 并发运行稳定（吞吐约 0.9~1.2 条/秒），20 条测试样本产出 17 found、3 absent、0 fail。
  - 代码已全部提交并推送至 GitHub 远端仓库 `main` 分支（`3a7574c`）。
- **下一步**：
  1. 维持 `auto_annotate.py` 8 并发运行，直至 Part 1 全部 2852 条待办预标处理完成。
  2. 启动 `server.py`，使用 `Alt + →` 和 `Enter` 快速流水线式审核 AI 预标框。
  3. Part 1 审核完毕后，使用 `make_manifest.py` 切换生成 Part 2 清单并推进后续标注。
