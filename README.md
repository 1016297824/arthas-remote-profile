# arthas-remote-profile

> 通过 SSH 登录远程主机或 Docker 容器，用 Arthas 对**正在运行的 JVM** 做在线性能诊断。
> 全程只读：不重启、不改代码、不改配置、不注入 agent。

产出是一份「关键路径 + 各环节耗时与占比 + 代码定位」的结论，可直接转成优化清单。

---

## 一、解决什么问题

线上接口莫名变慢（响应 > 1s 且没有明显错误日志），既不能重启复现，日志里也看不出来。
本技能把这类问题收敛成一条可重复的流程：

| 常见困境 | 本技能的解法 |
|---|---|
| 重启会清掉现场，不敢动 | 用 Arthas attach，全程只读采样 |
| 只看到「接口慢」，不知道慢在哪一层 | wall 墙钟火焰图，把 IO 等待也算进去 |
| 猜是不是 N+1，没有证据 | `monitor` / 采样调用树，拿到**真实调用次数** |
| 结论停在「哪个方法慢」 | 下钻到循环、SQL、代码行，落到可改的粒度 |

---

## 二、何时使用 / 何时不用

**用：**

- 用户反馈某接口或定时任务「很慢」「超时」「比以前慢」。
- 需要知道某方法被调用了多少次，判断 N+1、循环内查库、重复查询。
- 需要火焰图或调用树来替代「凭感觉猜」。
- 需要在不重启的前提下，对比某个改动前后的差异。

**不用：**

- 问题在 JVM 之外：网络、网关、DNS、磁盘 IO 打满、数据库自身慢查询（先看慢日志）。
- 排查内存泄漏 / GC —— 用 `heapdump` / `jstat` 更合适。
- 拿不到 SSH，或目标进程无法附着（如强制 `-XX:+DisableAttachMechanism`）。

---

## 三、目录结构

```
arthas-remote-profile/
├── README.md                     ← 本文件
├── SKILL.md                      ← 技能主体：流程、判读规则、常见坑
├── SECURITY-AUDIT.md             ← 供应链安全审计报告（P2 安全）
├── references/
│   └── arthas-commands.md        ← arthas 命令速查、HTTP API、报错对照
└── scripts/
    └── arthas_profiler.py        ← 单一入口脚本（521 行）
```

---

## 四、主流程（五步）

| 步骤 | 命令 | 说明 |
|---|---|---|
| 1. 探活 | `probe` | 列出 java 进程、HTTP API 是否就绪、`arthas-boot.jar` 位置 |
| 2. 附着 | `attach --pid <PID> --jar <jar>` | 后台启动 arthas，轮询 HTTP API 直到就绪 |
| 3. 采样 | `sample --trigger '<触发命令>' --out ./wall_collapsed.txt` | 一步完成「起采样 → 触发业务 → 停采样 → 下载」 |
| 4. 解析 | `top` / `tree` / `path` | 找热点 → 看调用树 → 回溯祖先链 |
| 5. 出结论 | — | 关键路径树 → 各环节毫秒与占比 → 类+方法根因 → 修复点 |

`sample` 的触发方式三选一（按可行性）：自己构造请求（需鉴权时注意凭据原子获取）→ 让用户点一次 → 等自然流量。

---

## 五、快速上手

### 1. 本机依赖（必须隔离安装，不要全局装）

```bash
python -m venv .venv
. .venv/bin/activate        # Linux/macOS
# .venv\Scripts\activate    # Windows
pip install paramiko
```

脚本**不会自动安装任何依赖** —— 缺 `paramiko` 时只报错退出。

### 2. 目标机前提

- 能 SSH 登录，有权限执行 `java` / `curl`、读写 `/tmp`。
- 目标机有 `curl`，且可以访问 arthas 监听端口（默认本机回环 8563 / 3658）。
- `arthas-boot.jar` 已存在，或可下载。

### 3. 常用命令

```bash
PY=python
S=scripts/arthas_profiler.py

# 探活
$PY $S probe

# 附着
$PY $S attach --pid <PID> --jar /path/to/arthas-boot.jar

# 一步采样（wall 墙钟，单位毫秒）
$PY $S sample \
  --trigger 'curl -s -X POST http://127.0.0.1:8080/your/endpoint \
             -H "Content-Type: application/json" -d "[\"<业务ID>\"]"' \
  --event wall \
  --out ./wall_collapsed.txt

# 解析
$PY $S top  ./wall_collapsed.txt --prefix com/yourcompany   # 找候选热点
$PY $S tree ./wall_collapsed.txt SomeService.doWork          # 看调用树
$PY $S path ./wall_collapsed.txt SomeMapper.selectPage       # 回溯祖先链
```

子命令全集：`probe` / `attach` / `sh` / `art` / `prof` / `sample` / `fetch` / `top` / `tree` / `path`。

### 4. 配置

可用命令行参数或环境变量：

| 环境变量 | 含义 | 默认 |
|---|---|---|
| `ARTHAS_SSH_HOST` | 目标机地址 | `localhost` |
| `ARTHAS_SSH_PORT` | SSH 端口 | `22` |
| `ARTHAS_SSH_USER` | SSH 用户 | `root` |
| `ARTHAS_SSH_PASSWORD` | SSH 密码（或用 `--env-file`） | 无 |
| `ARTHAS_API` | arthas HTTP API 地址（容器内可达） | 内置默认 |
| `ARTHAS_BOOT_JAR` | `arthas-boot.jar` 路径 | 内置默认 |

**优先走 HTTP API（8563），不要用 telnet（3658）** —— telnet 有 IAC 选项协商，脚本化时经常读不到提示符而假死。

---

## 六、判读的四条硬规则

读错这四条会把结论完全带偏：

1. **IO 等待只能用 wall 采样。** `-e cpu` 看不到等数据库、等 socket 的时间；慢接口十有八九卡在 IO，默认就用 `-e wall`。wall 的单位是墙钟毫秒，不是 CPU 时间。
2. **collapsed 数据是所有线程的合计。** `top` 里的总数可能比你的请求大两个数量级；**必须用 `tree <某个根方法>` 切开，只看目标链路**。
3. **区分「请求线程在等」和「工作线程在干」。** 请求线程 `join()` 阻塞 1330ms、工作线程同时干了 1330ms 的活，这两条**不能相加**。要从工作线程的根方法往下解析。
4. **采样点数 × 采样间隔 ≈ 耗时。** 默认 10ms 一点，所以 1 点 = 10ms，别把点数当毫秒直接读。

`@Async` 方法、定时任务线程、连接池预热线程都会出现在采样里。用 `path` 回溯祖先链：出现 `AsyncExecutionInterceptor` / `FutureTask.run` 说明它是并行的，通常不在关键路径上。

---

## 七、优先排查的四类反模式

拿到耗时排行后，先把候选热点往这四类上套 —— 传统 Java 单体里绝大多数「莫名慢」都落在其中：

| 反模式 | 采样中的典型形态 | 改法 |
|---|---|---|
| **循环内查库（N+1）** | `stream().map()` / `for` 里出现 `getOne` / `selectById` | 先收集 id 批量查，再在内存里分组匹配 |
| **该缓存不缓存** | 入参极少、结果稳定，却每次都查库 | 加应用级缓存，写操作时失效 |
| **只读查询开事务** | 出现 `TransactionInterceptor → commit → setAutoCommit → socket 读` | 只读方法去掉 `@Transactional` |
| **循环里反复拉全量** | 每轮都 `list()` 整表，只为取其中一条 | 缓存全量结果，或提到循环外只取一次 |

**判定依据不是「看起来像」，而是采样里它实际占了多少毫秒。**

---

## 八、安全与凭据

- **本流程只读。** `sh` 子命令与 `--trigger` 只用于触发被测业务 / 查看状态，**不要用它写数据、改配置、重启进程**。
- 凭据走环境变量或 `--env-file`，**不要硬编码进脚本、不要提交进仓库**。
- 凭据文件用完立即删除；密码若在对话、截图、日志里出现过，请更换。
- 采样会在目标机 `/tmp` 留下文件，诊断结束后执行 `stop` 退出 arthas 并清理临时文件。
- 详细审计结论见 [`SECURITY-AUDIT.md`](SECURITY-AUDIT.md)（P2 安全，无 P0 阻断项）。

---

## 九、部署位置

技能要在宿主里生效，需要被放到宿主的技能根目录下（即 `<技能根>/arthas-remote-profile/`）。

| 角色 | 说明 |
|---|---|
| **主副本** | 本仓库。作为唯一维护位置与可迁移快照，改技能只改这里 |
| **运行位** | 宿主实际加载技能的位置，把本仓库整个目录拷进去即可 |

> 宿主的技能发现规则是「技能根目录下的**一级子目录**含 `SKILL.md`」。
> 改技能请改**主副本**，再把整个目录拷到运行位覆盖，保持两边一致。

---

## 十、来源与状态

| 项 | 说明 |
|---|---|
| 创建 | 2026-09-17 |
| 定位 | 通用方法论，**不含任何实战案例、内网环境信息与业务标识** |
| 依赖 | Python 3 + `paramiko`（唯一第三方依赖，纯 PyPI 官方源） |
| 仓库 | 独立 git 仓库，可自行托管到任意 Git 服务 |
| 安全审计 | 2026-09-17 静态审计通过，评分 92/100，**P2 安全** |

> 本目录可能被推送到公开仓库。新增内容前请确认：无凭据值、无真实主机名/IP、无本地路径、无内部业务标识、无专有类名。
