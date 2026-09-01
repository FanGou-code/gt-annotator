# gt-annotator 前后端接口合同

> 本文件是前端（web/）与后端（server.py）之间的唯一接口真相。
> 后端已实现并测试；前端实现以本文为准，发现缺陷时在 `handoff.md` 记录，不要改后端。

## 基础约定

- 同源部署：前端由后端同一端口伺服，无 CORS、无 cookie。
- 所有 API 收发 JSON（UTF-8）；错误响应统一为 `{"error": "<message>"}`。
- 鉴权：启动参数 `--token SECRET` 时，`/api/*` 与 `/image` 必须带请求头 `X-Auth-Token: SECRET`，否则 `401`；静态前端资源（`web/` 下文件）**不需要** token（浏览器首次加载时还没有 token，由前端弹窗录入后访问 API）。未设 token 则无鉴权。
- 会话数据一次性下发：`/api/session` 包含全部条目（9555 条约 2-3MB），前端加载一次后自行在内存中维护游标与状态。

## 坐标系统（核心约定）

- bbox 为**归一化 0-1 XYXY**：`[x1, y1, x2, y2]`，原点在图片左上角，相对图片完整原始尺寸（`naturalWidth` / `naturalHeight`）。
- 换算责任在前端：屏幕像素 → 归一化用 `px_x / naturalWidth`；显示时反向乘回。
- 提交前保证 `x1 < x2`、`y1 < y2`；越界值服务端会裁剪到 `[0,1]`；完全出界或零面积的框返回 `400`。
- 建议前端强制最小拖拽尺寸（例如 4px）避免误出退化框。

## Endpoints

### GET /api/session

```json
{
  "manifest": "rgbdt-test",
  "total_items": 9555,
  "annotated": 1234,
  "items": [
    {
      "id": "000002_001",
      "image_url": "/image?src=Images%2Fvisible%2F000002_visible.jpg",
      "query_en": "the leftmost person in red",
      "query_zh": "穿红衣服的最左边的人",
      "bbox": [0.48, 0.43, 0.55, 0.51],
      "annotator": "alice"
    }
  ]
}
```

- `bbox`：`null`（未标注）或已保存的框。已标注条目回显其框。
- `query_zh`：翻译 sidecar 未加载时为 `null`。
- `annotator`：最近一次保存者，未标注为 `null`。

### GET /image?src=<urlencoded 相对路径>

- 返回图片字节；`src` 只接受 manifest 中出现过的路径字符串（防目录穿越），否则 `404`。
- 响应带 `Cache-Control: no-cache`。

### PUT /api/item/{id}/bbox

请求体：

```json
{"bbox": [0.48, 0.43, 0.55, 0.51], "annotator": "alice"}
```

- `annotator` 可省略，字符串最长 64 字符。
- 成功 `200`：`{"id": "...", "bbox": [...], "annotated": true}`（`bbox` 为服务端规范化后的值）。
- `400`：bbox 非法（形状/数值/零面积）；body 非法。
- `404`：item id 不在 manifest 中。

### DELETE /api/item/{id}/bbox

- 成功 `200`：`{"id": "...", "bbox": null, "annotated": false}`。
- 幂等；未标注条目也返回 200。`404` 同上。

### GET /api/progress

```json
{
  "manifest": "rgbdt-test",
  "total_items": 9555,
  "annotated": 1234,
  "total_images": 2000,
  "annotated_images": 980
}
```

## 静态文件

- `GET /` → `web/index.html`；其余路径在 `web/` 下解析，禁止越出该目录（越界或缺失 → `404`）。
- 前端尚未交付时 `GET /` 返回 `404 {"error": "frontend not built yet; web/index.html missing"}`。

## curl 冒烟示例

```bash
curl -H "X-Auth-Token: s3cret" http://127.0.0.1:8765/api/progress
curl -H "X-Auth-Token: s3cret" -X PUT http://127.0.0.1:8765/api/item/000002_001/bbox \
  -H "Content-Type: application/json" -d '{"bbox":[0.1,0.2,0.3,0.4],"annotator":"alice"}'
```
