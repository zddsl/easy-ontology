# Playground fork 说明（基于 Ontology-Playground-main，MIT）

本目录是 [Ontology-Playground](https://github.com/microsoft/Ontology-Playground) 的源码副本，
改动 **`src/App.tsx`** + **后端 viewer_enricher.py**，为 easy_ontology 增加「URL 参数加载本体」和「极简 viewer 模式」。

## 补丁内容（升级上游后需重打）

### 1. import（文件头部 shareCodec import 之后）
```ts
import { parseRDF } from './lib/rdf/parser';  // easy_ontology fork: ?rdf= URL 加载
```

### 2. localStorage 恢复守卫（"Restore the last loaded ontology" effect 内）
URL 带 `rdf` 参数时不恢复旧本体，避免覆盖外部喂入的本体：
```ts
const rdfUrlParam = new URLSearchParams(window.location.search).get('rdf');
if (rdfUrlParam || route.page === 'share' || ...) return;
```

### 3. 新增 mount effect（紧跟恢复 effect之后）
读 `?rdf=<url>&label=<名>` → fetch RDF/XML 文本 → `parseRDF(text, {inferIdentifiers: true})`
→ `loadOntology(ontology, bindings, {label})`；失败 toast 中文报错。
模式抄自 `ImportExportModal.tsx` 的导入逻辑。

### 4. viewer 模式 + 类节点调色板（2026-08-23）

**App.tsx 改动**：
- 组件开头：检测到 `?rdf=` 参数时 `isViewerMode = true`
- 渲染条件：Header / GuidedTour / OntologyStatsPanel / SearchFilter / QueryPlayground 只在非 viewer 模式渲染
- **PathFinderPanel 与 InspectorPanel 始终保留**（用户点名要 find path 和点击查看）
- app-container 加 `viewer-mode` class
- viewer 模式暴露 `window.__eoSelect(entityId)` 测试钩子（agent-browser 无法对 canvas 坐标点击，E2E 用它选中实体）
- 警告 toast 在 viewer 模式只进 console 不弹窗

**app.css 改动**（两个关键坑）：
1. `.app-container` 是 `grid-template-rows: 64px 1fr`，64px 是 Header 行；Header 不渲染后图被塞进 64px 行压扁 → viewer-mode 改 `1fr`
2. **上游 ≤900px 媒体查询会把容器切 flex 单列、`.right-sidebar{display:none}`、图例藏掉**——我们 iframe 内宽常 <900px 必中招 → viewer-mode 用更高优先级选择器反制：`display:grid` + `grid-template-columns: 1fr 300px` + sidebar `display:flex` + legend `display:block` + 移动标签栏 `display:none`

**后端 viewer_enricher.py 改动**（`backend/services/viewer_enricher.py`）：
- 注入 `<color>`：对每个 OWL Class 按稳定索引注入（Tableau 10 前 8 色，本体自带不覆盖），parser.ts 无命名空间读 localName=color
- **注入 `rdfs:domain`**：`derive_domains()` 从胖映射 target 行 `tmpl a :Class ; :prop "字面量"` 反推数据属性归属（本体删过 domain 时 Inspector 显示不出属性）；多类共用属性 setdefault 取首个
- 已有 `rdfs:range` 反推（derive_ranges）不变

**效果**：
- iframe 里只显示本体拓扑图 + 检查器，无导航栏/tour/stats
- 每个类节点颜色互不相同（5 个类自动分配 5 色）

## 构建

正式路径（容器内，依赖不落宿主机）：
```
docker compose --profile build run --rm playground-builder
```
产物：`build/` → 拷贝到 `../backend/static/playground/`，由 FastAPI 挂载在 `/playground/`。

**注意**：Python 改动需 dev 模式（bind-mount `./backend:/app/backend`）或重建 prod 镜像。Playground 构建产物直接覆盖 static 目录，dev/prod 共用。

iframe 用法：
```
/playground/?rdf=${encodeURIComponent("/api/ontology/rdf?viewer=1")}&label=hd&t=${timestamp}%23/
```
`viewer=1` 是后端从映射反推 range + 注入颜色的增强副本。

## 许可

上游 LICENSE（MIT, © Microsoft Corporation）保留于本目录；Microsoft 商标条款注意别复用其 logo/品牌。
