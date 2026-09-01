# gt-annotator — 通用人工定位打标器

> 本项目为 RGBDT 三模态视觉定位竞赛项目开发的人工标注工具，通用化后独立开源。

给「图片 + 指代 query」人工画出目标框的小工具：浏览器里鼠标画框，产出与主流
预测文件同构的标注（`{条目id: [x1, y1, x2, y2]}`，归一化 0-1 XYXY），可直接
喂给任何评测/融合脚本。后端纯 Python 标准库，**零 pip 依赖**；前端原生
HTML/JS/CSS 单页，完全离线可用。

## 功能

- canvas 画框 → 拖拽移动 → 8 控制点缩放；滚轮缩放、拖拽平移，适配小目标
- 双语 query 并列展示（可选，自带批量翻译或自带译文均可）
- 图号跳转、逐题快捷键流（`Enter` 提交并跳下一条未标注、`Esc` 清除、`←`/`→` 切题）
- 崩溃安全：追加日志 + 原子快照，重启自动恢复；多人各标一份可合并
- token 鉴权（可选），支持局域网/远程访问

## 30 秒体验（自带样例数据）

```bash
python3 server.py --manifest examples/manifest.example.json \
    --translations examples/translations.example.json --port 8765
```

浏览器打开 `http://localhost:8765/`，给 4 条样例 query 画框即可。样例含
`examples/images/` 三张演示图与两种 query 源格式（`queries.mapping.example.json`
映射式 / `queries.list.example.json` 列表式）。

## 接入你自己的数据

### 1. 生成 manifest

query 源支持两种 JSON 形状：

```json
// 映射式：{"<id>": {"<图片字段>": "相对图片根目录的路径", "<query字段>": "..."}}
{"img_001": {"image": "photos/a.jpg", "query": "the red car"}}

// 列表式：[{"id": "img_001", "image": "photos/a.jpg", "query": "the red car"}]
```

```bash
python3 make_manifest.py \
    --source /路径/你的queries.json \
    --images-root /路径/你的图片目录/ \
    --image-field image --query-field query \
    --out data/manifest.json
# --image-field/--query-field 按 your JSON 的字段名改；注意 \ 必须是行尾最后一个字符，注释只能单独成行
# 自动校验每张图存在，缺图会列出；--skip-image-check 可跳过
```

### 2.（可选）翻译 query 为中文

已有译文？跳过本步，直接给 server 传 `--translations 你的译文.json`
（格式 `{"<id>": "中文", ...}`，参考 `examples/translations.example.json`）。

没有译文可用内置批量翻译（任意 OpenAI 兼容端点）：

```bash
export API_KEY=sk-你的密钥          # 只走环境变量，永不入库
python3 translate.py --manifest data/manifest.json \
    --out data/translations.json --concurrency 8 -v
# --model / --base-url 可换供应商；断点续跑，重跑自动跳过已翻条目
# -v 逐条打印译文；另开终端 tail -f data/translations.jsonl 实时围观
```

### 3. 启动服务并标注

```bash
python3 server.py --manifest data/manifest.json --data-dir data \
    --host 0.0.0.0 --port 8765 --token 自定密码
```

浏览器打开 `http://<主机IP>:8765/`，首次访问在弹窗填入 token（存浏览器
localStorage，之后免填）。跨机器访问推荐 SSH 隧道：
`ssh -N -L 8765:127.0.0.1:8765 user@主机`，浏览器用 `localhost:8765`。

## 输出说明

| 文件 | 内容 |
| --- | --- |
| `data/annotations.predictions.json` | **最终产物**：`{条目id: [x1,y1,x2,y2]}` 归一化 0-1 XYXY，每次保存原子重写，可直接被评测/融合脚本消费 |
| `data/annotations.jsonl` | 追加日志：每行含 `id / bbox / annotator / ts`，崩溃恢复依据；多人各标一份后可按 id 合并 |
| `data/manifest.json` | 输入清单（由 make_manifest.py 生成） |
| `data/translations.json(l)` | 翻译快照与追加日志 |

坐标约定：bbox 为归一化 0-1 XYXY，原点在图片左上角，相对图片原始尺寸
（`naturalWidth/Height`）。接口细节见 [API.md](API.md)。

## 开发

```bash
python3 -m unittest discover -s tests   # 全量单测，纯标准库
```

代码结构：`server.py`（HTTP 服务与路由）/ `store.py`（JSONL 日志 + 快照原子写）/
`bbox.py`（坐标校验与归一化）/ `translate.py`（批量翻译）/ `make_manifest.py`
（清单构建）/ `web/`（前端单页）。

## License

[MIT](LICENSE)
