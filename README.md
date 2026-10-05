# 邮件会话重建 API（Email Conversation Reconstruction API）

导入原始 EML 邮件批次，解析 `Message-ID` / `In-Reply-To` / `References`，按邮箱独立重建
会话树；缺失父件先占位，迟到父件到达时归并会话；每次**有效变更**原子形成一个**修订（revision）**，
可读取任意历史修订当时的会话树与缺件。

技术栈：Python 3.11 · FastAPI · PostgreSQL 15 · 标准库 `email`。

## 启动

需要 Docker 与 Docker Compose（v2）。

```bash
docker compose up --build
```

启动后：

| 服务 | 地址 |
| --- | --- |
| API 服务 | <http://localhost:8000> |
| 健康检查 | <http://localhost:8000/health> |
| 交互式文档（Swagger UI） | <http://localhost:8000/docs> |
| OpenAPI 描述 | <http://localhost:8000/openapi.json> |
| PostgreSQL | `localhost:5432`（库/用户/密码均为 `mails`，容器间主机名为 `db`） |

数据保存在 Docker 卷 `pgdata` 中；`docker compose down -v` 可清空。

## 线程模型

* 每个被提及的 Message-ID 在一个邮箱内是一个**节点**；节点要么是真实邮件（存原始 EML 字节），
  要么是**占位缺件**。
* 每节点至多一个父指针：优先取 `In-Reply-To`；没有时取 `References` 的**最后一个**标识。
* **会话编号 = 根节点的 Message-ID**。
* 父件缺失时创建占位节点；邮件先到、引用链中间缺失时，按 References 顺序推断中间占位节点的父子关系。
* 真实邮件到达后若证实占位节点的父边不对，则纠正该边——这会把已存子树挂到新根上，即**会话归并**，
  响应里的 `merged` 返回「旧会话编号 → 新会话编号」映射。
* 真实已存邮件的内容与父边不可变：同 ID 以不同字节重投会**整批拒绝**。

## API

### `POST /imports` — 导入一批 EML

`multipart/form-data`：

| 字段 | 说明 |
| --- | --- |
| `mailbox` | 邮箱标识；各邮箱数据与修订号完全独立 |
| `messages` | 一个或多个原始 EML 文件（可重复字段）。**批次内顺序无关**，最终树相同 |

同 `Message-ID` 且**原文字节完全一致**的批次重投：不推进修订，`duplicate=true` 并返回原修订号
（及当时的归并映射）。整批中任一封出现以下情况都会 **HTTP 422 整批拒绝、不推进修订**：

* 缺少/无法解析 `Message-ID`（`malformed_eml`）
* 同 ID 但字节不同（批内或与已存件冲突，`conflicting_content`）
* 自引用：In-Reply-To 或 References 指向自己（`self_reference`）
* 与已有节点构成父子环（`cycle`）

#### 例 1：回复先到，父件缺失（创建占位缺件，修订 1）

```bash
cat > reply.eml <<'EOF'
Message-ID: <reply-42@example.com>
In-Reply-To: <ghost@example.com>
Subject: Re: Project plan

body
EOF

curl -s -X POST http://localhost:8000/imports \
  -F mailbox=demo -F messages=@reply.eml
```

```json
{
  "mailbox": "demo",
  "revision": 1,
  "effective": true,
  "duplicate": false,
  "merged": {},
  "imported": 1,
  "new_messages": 1,
  "filled_placeholders": 0,
  "placeholders_created": 1
}
```

#### 例 2：迟到的父件到达并声明真正的根（修订 2，发生会话归并）

```bash
cat > ghost.eml <<'EOF'
Message-ID: <ghost@example.com>
In-Reply-To: <thread-root@example.com>
Subject: Re: Project plan

body
EOF

curl -s -X POST http://localhost:8000/imports \
  -F mailbox=demo -F messages=@ghost.eml
```

```json
{
  "mailbox": "demo",
  "revision": 2,
  "effective": true,
  "duplicate": false,
  "merged": {
    "ghost@example.com": "thread-root@example.com"
  },
  "imported": 1,
  "new_messages": 0,
  "filled_placeholders": 1,
  "placeholders_created": 1
}
```

`ghost@example.com` 子树（含 reply-42）的旧会话编号 `ghost@example.com` 归并到新编号
`thread-root@example.com`。

#### 例 3：原文字节重投 → 返回原修订，不推进

```bash
curl -s -X POST http://localhost:8000/imports \
  -F mailbox=demo -F messages=@ghost.eml
```

```json
{
  "mailbox": "demo",
  "revision": 2,
  "effective": false,
  "duplicate": true,
  "merged": {
    "ghost@example.com": "thread-root@example.com"
  },
  "imported": 1
}
```

### `GET /threads` — 读取会话树

查询参数：

| 参数 | 说明 |
| --- | --- |
| `mailbox` | 邮箱标识（必填） |
| `revision` | 可选，读取该修订时点；省略则读最新修订 |

补上根邮件后读取最新树：

```bash
printf 'Message-ID: <thread-root@example.com>\nSubject: Project plan\n\nbody\n' > root.eml
curl -s -X POST http://localhost:8000/imports -F mailbox=demo -F messages=@root.eml

curl -s "http://localhost:8000/threads?mailbox=demo"
```

```json
{
  "mailbox": "demo",
  "revision": 3,
  "latest_revision": 3,
  "threads": [
    {
      "thread_id": "thread-root@example.com",
      "root_missing": false,
      "tree": {
        "message_id": "thread-root@example.com",
        "missing": false,
        "children": [
          {
            "message_id": "ghost@example.com",
            "missing": false,
            "children": [
              {
                "message_id": "reply-42@example.com",
                "missing": false,
                "children": []
              }
            ]
          }
        ]
      }
    }
  ],
  "missing": []
}
```

读取**修订 1**（ghost 仍缺件、尚未归并）；历史修订不会被后来的归并改写：

```bash
curl -s "http://localhost:8000/threads?mailbox=demo&revision=1"
```

```json
{
  "mailbox": "demo",
  "revision": 1,
  "latest_revision": 3,
  "threads": [
    {
      "thread_id": "ghost@example.com",
      "root_missing": true,
      "tree": {
        "message_id": "ghost@example.com",
        "missing": true,
        "children": [
          {
            "message_id": "reply-42@example.com",
            "missing": false,
            "children": []
          }
        ]
      }
    }
  ],
  "missing": [
    "ghost@example.com"
  ]
}
```

错误：未知邮箱 / 未知修订返回 `404`（`detail.code` 分别为 `unknown_mailbox`、
`unknown_revision`）。

### 整批拒绝示例（自引，422，修订不推进）

```bash
printf 'Message-ID: <bad@example.com>\nIn-Reply-To: <bad@example.com>\n\nx\n' > bad.eml
curl -s -X POST http://localhost:8000/imports -F mailbox=demo -F messages=@bad.eml
```

```json
{
  "error": {
    "code": "rejected",
    "details": [
      {
        "index": 0,
        "code": "self_reference",
        "message_id": "bad@example.com",
        "message": "message references its own Message-ID"
      }
    ]
  }
}
```

## 并发与原子性

* 每个导入在单事务内完成：解析后先在 Python 中校验，再在数据库中以
  `pg_advisory_xact_lock(邮箱)` 串行化同邮箱写入；不同邮箱互不阻塞。
* 成环检测覆盖「库内已存深链 + 本批新边」的组合（沿父边用递归 CTE 追溯）。
* 修订历史为只追加表 `link_events`：每次边的建立/纠正记一条事件，读取修订 N 时取每个节点
  `rev <= N` 的最近事件重建森林；历史修订永不 UPDATE。

## 性能

* 每封邮件只按头部解析（不触正文），导入通常是 1 次批量 SELECT + 若干参数化写入，
  单封补件的祖先检测在「挂到叶节点」常见路径下**不查库**（无 override 的纯追加边）。
* 基准脚本 `bench/bench.py`（用嵌入式 PostgreSQL）：播种 100,000 封（约 8,000 个线程）后，
  对 ≤8 KiB 单封补件计时。在本机（2 核受限环境）实测：

| 操作 | 实测耗时 |
| --- | --- |
| 播种 100,000 封（1000 封/批） | ~72 s（约 1,400 封/秒） |
| **单封 ≤8 KiB 补件导入** | **约 3 ms（SLA ≤ 2,000 ms）** |
| `GET /threads` 全量最新（7.1 MB JSON，8,000 线程） | ~0.5 s |
| `GET /threads?revision=51` 历史重建 | ~0.5 s |

```bash
python bench/bench.py --messages 100000
```

## 本地开发与测试

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt

# 测试使用嵌入式 PostgreSQL（pgserver 自动下载二进制），无需本机安装 PG
pytest -q

# 指定外部 PostgreSQL 运行 API
DATABASE_URL=postgresql://mails:mails@localhost:5432/mails \
  uvicorn app.main:app --reload
```

## 目录结构

```
app/
  schema.sql    PostgreSQL 表结构（mailboxes / nodes / revisions / link_events）
  eml.py        标准库 email 解析 Message-ID / In-Reply-To / References（CFWS 注释/折行）
  importer.py   批次校验、事务、占位、归并、成环检测、修订与历史重建
  db.py         连接池与建表
  main.py       FastAPI 路由
tests/          基于真实 PostgreSQL 的集成测试
bench/          十万封性能基准与 README 示例抓取脚本
```
