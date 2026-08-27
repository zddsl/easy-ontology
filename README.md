# Easy Ontology

**一个容器，把关系数据库变成知识图谱：可视化搭建本体与映射 → 直接查询/自然语言问数。内置 REST API，分钟级接入 Dify 等工作流，定制你的智能问数应用。**

Self-hosted OBDA (Ontology-Based Data Access) platform in a single Docker container: visually build OWL ontologies & R2RML mappings, query any relational database with SPARQL or plain natural language, and integrate into Dify / LLM workflows via ready-made REST APIs. Both query engines ([Ontop](https://ontop-vkg.org/) virtual + [QLever](https://qlever.cs.uni-freiburg.de/) materialized) are built in — nothing else to deploy or manage.

![License](https://img.shields.io/badge/license-MIT-blue)
![Docker](https://img.shields.io/badge/docker-one%20container-2496ED)
![Ontop](https://img.shields.io/badge/Ontop-5.5-8A2BE2)
![LLM](https://img.shields.io/badge/LLM-DeepSeek-4D6BFE)

---

## 为什么是 Easy Ontology？

传统 OBDA 工具链的门槛在于：要用 Protégé 建本体、手写 OBDA 映射文件、命令行起 Ontop、再自己接查询前端——四件事四个工具。Easy Ontology 把它们收进一个 Web 界面：

- **🎨 平台搭建本体** — 不装 Protégé，浏览器里建类、对象属性、数据属性，支持公理，实时拓扑图预览
- **🔗 平台搭建映射** — 三步向导：选表 → 系统按外键关系自动推荐 JOIN → 生成 OBDA 映射
- **⚡ 查询引擎全自动** — Ontop 与 QLever 全部内置同一容器：端点查询时自动拉起，ABox 变化后索引自动重建，**用户不需要部署、启动、维护任何引擎**
- **💬 自然语言问数** — 配一个 DeepSeek API Key，中文提问 → 自动生成 SPARQL → 执行 → 回人话答案
- **🗂️ 多工作空间** — 多套本体/映射/数据源隔离，一键切换
- **🔌 对外 REST API** — `/sparql`、`/api/ask`、`/api/ontology/summary`，Dify / 外部系统改个 URL 就能接

支持 **MySQL** 与 **达梦 DM8**（国产数据库友好）。

## 架构

**单容器全栈**——查询引擎、索引守护全部内置，整个系统只有一个容器：

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
│  │ · 查询登记簿/行数截断     │   │ (SqlStream+并行)  │  │
│  │ · LLM 三跳问答          │   └────────┬──────────┘  │
│  └────┬──────────────┬───┘            │ abox.nt     │
│       │              │                ▼             │
│  ┌────▼─────────┐    │    ┌────────────────────────┐ │
│  │ Ontop (内置)  │    └───►│ QLever (内置)           │ │
│  │ 查询时自动拉起 │         │ 守护进程自动重建索引     │ │
│  └────┬─────────┘         │ :7001（仅容器内部）      │ │
│       │                   └────────────────────────┘ │
└───────┼─────────────────────────────────────────────┘
        ▼ SQL (JDBC)
   ┌─────────┐
   │ MySQL / │  ← 你已有的数据库，数据不动
   │  DM8    │
   └─────────┘
```

两条引擎由平台全自动管理，用户零操作：

| 引擎 | 工作方式 | 适合场景 |
|------|---------|---------|
| **Ontop**（虚拟） | 查询实时翻译成 SQL 直查数据库，不落盘 | 数据频繁变化，要最新结果 |
| **QLever**（物化） | 「③ 生成 ABox」把数据物化成三元组，索引秒级图查询；数据变化重新生成即可，索引自动重建 | 大数据量、复杂关联分析 |

## 快速开始

### 方式一：拉镜像一键起（推荐）

```bash
docker run -d --name easy-ontology \
  -p 8010:8000 \
  -v easy-ontology-data:/app/data \
  --add-host=host.docker.internal:host-gateway \
  --restart unless-stopped \
  zddsl/easy-ontology:latest
```

打开 `http://localhost:8010` 即可使用。数据（本体/映射/配置）持久化在 `easy-ontology-data` 卷里。

> 数据库在宿主机上？host 填 `host.docker.internal` 即可连通。

### 方式二：源码构建

```bash
git clone https://github.com/zddsl/easy-ontology.git
cd easy-ontology
docker compose --profile prod up -d --build
```

### 首次使用三步

1. **① 数据源** — 填 MySQL/DM8 连接信息，点「测试连接」
2. **② 本体与映射** — 上传现成的 `.rdf`/`.obda`，或切到「平台搭建」在浏览器里可视化搭建
3. **查询** — 直接写 SPARQL；或在底部「自然语言问答」用中文提问（需先在顶栏「配置」里填 DeepSeek API Key）

想要物化加速？点一下「③ 生成 ABox」即可，索引自动构建，无需任何手动维护。

## 自然语言问答

顶栏「配置」→ 填入 [DeepSeek API Key](https://platform.deepseek.com/)，然后就可以：

> 「有多少名员工？」「工资超过 20000 的都有谁？」

系统自动完成三跳：问题 → SPARQL → 执行 → 自然语言回答。生成的 SPARQL 与 CSV 结果可展开查看，方便核对。

## 对外 API

平台本身就是个查询网关，外部系统（Dify、脚本、大屏）直接调用：

**SPARQL 查询**（Ontop 协议兼容；`route` 可省略，默认 `virtual`，生成过 ABox 后可用 `materialized`）

```bash
curl -X POST http://localhost:8010/sparql \
  -H "Accept: text/csv" \
  -d "query=SELECT ?s WHERE { ?s ?p ?o } LIMIT 10"
```

**自然语言问答**

```bash
curl -X POST http://localhost:8010/api/ask \
  -H "Content-Type: application/json" \
  -d '{"question": "有多少条工单？"}'
```

**本体摘要**（喂给外部 LLM 做提示词）

```bash
curl http://localhost:8010/api/ontology/summary
# → { "ns": ..., "classes": [...], "obj_props": [...], "dt_props": [...] }
```

完整接口与参数说明见页面顶栏 **API** 按钮。

## 查询引擎：全自动，零维护

- **开箱即查** — 容器起来就能查询（虚拟引擎，实时翻译 SQL）
- **一键物化** — 点「③ 生成 ABox」，千万级三元组约 7 分钟（自研并行引擎），索引随后**自动构建**
- **自动跟随** — 数据变化重新生成、上传新 ABox、切换工作空间，索引都会自动重建，全程无需手动操作

> 进阶：想用外置 QLever（如已有的独立集群）？挂载自定义 `config.yaml` 覆盖 `rdf_store.base_url` 即可，参考 `deploy/qlever/watch-abox.sh`（外置看守版）。

## 开发模式

```bash
docker compose --profile dev up --build
```

源码 bind mount + uvicorn 热重载；Playground 前端改动用 `--profile build` 重新构建。

## 发布镜像（维护者）

```bash
docker build --target prod -t zddsl/easy-ontology:latest .
docker push zddsl/easy-ontology:latest
```

## 致谢

- [Ontop](https://ontop-vkg.org/) (Apache-2.0) — 虚拟知识图谱引擎，`ontop-cli-5.5.0/` 随仓库分发
- [QLever](https://qlever.cs.uni-freiburg.de/) (Apache-2.0) — 超高性能 RDF 索引与查询，二进制取自官方镜像内置分发
- [WebVOWL / Ontology Playground](https://github.com/VisualDataWeb/Ontology-Playground) — 拓扑图可视化

## License

[MIT](LICENSE)
