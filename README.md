# Easy Ontology

**一个容器，把关系数据库变成知识图谱：可视化搭建本体与映射 → SPARQL 双路线查询 → 自然语言直接问数。内置 REST API，分钟级接入 Dify 等工作流，定制你的智能问数应用。**

Self-hosted OBDA (Ontology-Based Data Access) platform in a single Docker container: visually build OWL ontologies & R2RML mappings, query any relational database with SPARQL (virtual via [Ontop](https://ontop-vkg.org/), or materialized via [QLever](https://qlever.cs.uni-freiburg.de/)), and ask questions in plain natural language powered by LLM — with ready-made REST APIs for quick integration into Dify / LLM workflows and custom development.

![License](https://img.shields.io/badge/license-MIT-blue)
![Docker](https://img.shields.io/badge/docker-one%20container-2496ED)
![Ontop](https://img.shields.io/badge/Ontop-5.5-8A2BE2)
![LLM](https://img.shields.io/badge/LLM-DeepSeek-4D6BFE)

---

## 为什么是 Easy Ontology？

传统 OBDA 工具链的门槛在于：要用 Protégé 建本体、手写 OBDA 映射文件、命令行起 Ontop、再自己接查询前端——四件事四个工具。Easy Ontology 把它们收进一个 Web 界面：

- **🎨 平台搭建本体** — 不装 Protégé，浏览器里建类、对象属性、数据属性，支持公理，实时拓扑图预览
- **🔗 平台搭建映射** — 三步向导：选表 → 系统按外键关系自动推荐 JOIN → 生成 OBDA 映射
- **⚡ 虚拟路线（开箱即用）** — Ontop 端点内嵌容器，查询时自动拉起，SQL 实时转 RDF，数据不落盘
- **📦 物化路线（可选）** — 自研并行 ABox 生成引擎，千万级三元组约 7 分钟，QLever 秒级查询
- **💬 自然语言问数** — 配一个 DeepSeek API Key，中文提问 → 自动生成 SPARQL → 执行 → 回人话答案
- **🗂️ 多工作空间** — 多套本体/映射/数据源隔离，一键切换
- **🔌 对外 REST API** — `/sparql`、`/api/ask`、`/api/ontology/summary`，Dify / 外部系统改个 URL 就能接

支持 **MySQL** 与 **达梦 DM8**（国产数据库友好）。

## 架构

```
┌─────────────────────────────────────────────────────┐
│  easy-ontology 容器 (:8010)                          │
│                                                     │
│  ┌──────────┐  ┌──────────┐  ┌───────────────────┐  │
│  │ Web 控制台 │  │ REST API │  │ 本体/映射搭建页    │  │
│  └────┬─────┘  └────┬─────┘  └───────────────────┘  │
│       │             │                                │
│  ┌────▼─────────────▼─────┐   ┌───────────────────┐  │
│  │ FastAPI 后端            │   │ ABox 生成引擎      │  │
│  │ · 懒启动管理 Ontop 子进程 │   │ (SqlStream+并行)  │  │
│  │ · 查询登记簿/行数截断     │   └────────┬──────────┘  │
│  │ · LLM 三跳问答          │            │ abox.nt     │
│  └────┬──────────────┬───┘            ▼             │
│       │ 虚拟路线       │ 物化路线   ┌────────┐         │
│  ┌────▼─────┐        └──────────►│ QLever │(可选容器)│
│  │  Ontop   │                    └────────┘ :7001   │
│  └────┬─────┘                                       │
└───────┼─────────────────────────────────────────────┘
        ▼ SQL (JDBC)
   ┌─────────┐
   │ MySQL / │  ← 你已有的数据库，数据不动
   │  DM8    │
   └─────────┘
```

**虚拟路线**：SPARQL → Ontop 实时翻译成 SQL 查库，适合数据频繁变化的场景，零额外容器。
**物化路线**：先把数据生成为 RDF 三元组文件，QLever 建索引查询，适合大数据量、复杂图查询。

## 快速开始

### 方式一：拉镜像一键起（推荐）

```bash
docker run -d --name easy-ontology \
  -p 8010:8000 \
  -v easy-ontology-data:/app/data \
  --add-host=host.docker.internal:host-gateway \
  --restart unless-stopped \
  <your-dockerhub-username>/easy-ontology:latest
```

打开 `http://localhost:8010` 即可使用。数据（本体/映射/配置）持久化在 `easy-ontology-data` 卷里。

> 数据库在宿主机上？host 填 `host.docker.internal` 即可连通。

### 方式二：源码构建

```bash
git clone https://github.com/<your-github-username>/easy-ontology.git
cd easy-ontology
docker compose --profile prod up -d --build
```

### 首次使用三步

1. **① 数据源** — 填 MySQL/DM8 连接信息，点「测试连接」
2. **② 本体与映射** — 上传现成的 `.rdf`/`.obda`，或切到「平台搭建」在浏览器里可视化搭建
3. **查询** — 直接写 SPARQL；或在底部「自然语言问答」用中文提问（需先在顶栏「配置」里填 DeepSeek API Key）

## 自然语言问答

顶栏「配置」→ 填入 [DeepSeek API Key](https://platform.deepseek.com/)，然后就可以：

> 「有多少名员工？」「工资超过 20000 的都有谁？」

系统自动完成三跳：问题 → SPARQL → 执行 → 自然语言回答。生成的 SPARQL 与 CSV 结果可展开查看，方便核对。

## 对外 API

平台本身就是个查询网关，外部系统（Dify、脚本、大屏）直接调用：

**SPARQL 查询**（Ontop 协议兼容）

```bash
curl -X POST http://localhost:8010/sparql \
  -H "Accept: text/csv" \
  -d "query=SELECT ?s WHERE { ?s ?p ?o } LIMIT 10" \
  -d "route=virtual"
```

**自然语言问答**

```bash
curl -X POST http://localhost:8010/api/ask \
  -H "Content-Type: application/json" \
  -d '{"question": "有多少条工单？", "route": "virtual"}'
```

**本体摘要**（喂给外部 LLM 做提示词）

```bash
curl http://localhost:8010/api/ontology/summary
# → { "ns": ..., "classes": [...], "obj_props": [...], "dt_props": [...] }
```

完整接口与参数说明见页面顶栏 **API** 按钮。

## 物化路线（可选，进阶）

需要额外部署一个 QLever 容器，用于「物化」查询模式与大体积 ABox：

1. 准备 QLever 容器（镜像含 `qlever-index` / `qlever-server`），挂载两个目录：
   - 平台数据目录 → `/abox:ro`
   - 索引工作目录 → `/data`
2. 容器内运行看守脚本（ABox 变化自动重建索引）：
   ```bash
   # 脚本在本仓库 deploy/qlever/watch-abox.sh
   docker exec -d <qlever容器> bash /data/watch-abox.sh
   ```
3. 平台 `config.yaml` 里确认 `rdf_store.base_url` 指向 QLever 端点（默认 `http://host.docker.internal:7001`）

之后在页面「③ 生成 ABox」点生成，索引就绪即可切到物化路线查询。

## 开发模式

```bash
docker compose --profile dev up --build
```

源码 bind mount + uvicorn 热重载；Playground 前端改动用 `--profile build` 重新构建。

## 发布镜像（维护者）

```bash
docker build --target prod -t <your-dockerhub-username>/easy-ontology:latest .
docker push <your-dockerhub-username>/easy-ontology:latest
```

## 致谢

- [Ontop](https://ontop-vkg.org/) (Apache-2.0) — 虚拟知识图谱引擎，`ontop-cli-5.5.0/` 随仓库分发
- [QLever](https://qlever.cs.uni-freiburg.de/) — 超高性能 RDF 索引与查询
- [WebVOWL / Ontology Playground](https://github.com/VisualDataWeb/Ontology-Playground) — 拓扑图可视化

## License

[MIT](LICENSE)
