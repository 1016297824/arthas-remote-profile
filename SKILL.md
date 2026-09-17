---
name: arthas-remote-profile
description: 通过 SSH 登录远程主机或 Docker 容器，用 Arthas 对运行中的 JVM 做在线性能诊断——在「不重启、不改代码、不注入 agent」的前提下，用墙钟火焰图、trace、monitor 定位慢接口/慢任务的热点方法与真实调用次数，并把耗时拆解到具体代码行。当用户反馈某个接口或定时任务「很慢」「超时」「耗时异常」，需要在不重启服务的情况下找出耗时究竟花在哪个方法、哪条 SQL、哪个循环上时使用。适用于任何 SSH 可达、且能用 java -jar arthas-boot.jar 附着的 JVM 进程，尤其 Java 8 + Spring Boot + MyBatis 的传统单体。
agent_created: true
---

# Arthas 远程在线性能诊断

## 用途

对**已经在跑**的 JVM 做性能诊断，全程只读：采样、trace、monitor。不重启、不改代码、不改配置、不注入 agent。
产出是一份「关键路径 + 各环节耗时与占比 + 代码定位」的结论，可直接转成优化清单。

## 何时使用

- 用户说某接口/任务「很慢」「超时」「比以前慢」；响应时间 > 1s 且没有明显错误日志。
- 需要知道某方法**被调用了多少次**（判断 N+1、循环内查库、重复查询）。
- 需要一份**火焰图或调用树**来替代「凭感觉猜」。
- 需要在**不重启**的前提下验证某个改动前后的差异（改完部署后再采一次对比）。

## 何时不用

- 问题在 JVM 之外：网络、网关、DNS、磁盘 IO 打满、DB 自身慢查询（先看慢日志）。
- 目标是排查内存泄漏/GC —— 用 `heapdump` / `jstat` 更合适，本流程不覆盖。
- 拿不到 SSH，或目标进程无法附着（如强制 `-XX:+DisableAttachMechanism`）。

## 前提

| 项 | 要求 |
|---|---|
| SSH | 能登录目标机/容器，有权限执行 `java`、`curl`、读写 `/tmp` |
| 目标机 | 有 `curl`；能访问 arthas 监听端口（默认 8563/3658，都在本机回环） |
| arthas | `arthas-boot.jar` 已存在于目标机，或可下载 |
| 本机 | Python 3 + `paramiko`（见下方安装） |

### 本机依赖安装

**必须装在隔离环境里，不要全局安装：**

```bash
python -m venv .venv
. .venv/bin/activate        # Linux/macOS
# .venv\Scripts\activate    # Windows
pip install paramiko
```

脚本**不会自动安装任何依赖** —— 缺 `paramiko` 时只会报错退出，不会偷偷执行 `pip install`。

## 主流程（五步）

### 第 1 步：探活

```bash
python scripts/arthas_profiler.py probe
```

输出目标机上的 java 进程、arthas HTTP API 是否已就绪、telnet 3658 是否开放、`arthas-boot.jar` 在哪。
若 arthas 未启动，用第 2 步附着。

### 第 2 步：附着 arthas

```bash
python scripts/arthas_profiler.py attach --pid <目标JVM的PID> --jar /path/to/arthas-boot.jar
```

脚本会 `nohup java -jar arthas-boot.jar <pid>` 后台启动，并轮询 HTTP API 直到就绪。
**优先走 HTTP API（8563），不要用 telnet（3658）** —— telnet 有 IAC 选项协商，脚本化时经常读不到提示符而假死。

### 第 3 步：一步完成「起采样 → 触发业务 → 停采样 → 下载」

```bash
python scripts/arthas_profiler.py sample \
  --trigger 'curl -s -X POST http://127.0.0.1:8080/article/publish \
             -H "Content-Type: application/json" -d "[\"<业务ID>\"]"' \
  --event wall \
  --out ./wall_collapsed.txt
```

`--trigger` 就是「让被测路径跑一次」的任意 shell 命令。脚本会先停掉上一次采样、重开一个干净的采样窗口，
执行触发命令、等待 `--settle` 秒、再停止采样并下载 collapsed 文本。

**触发方式三选一**（按可行性排序）：

1. **自己构造请求**：需要鉴权时，优先找应用自己缓存的凭据。若应用把 JWT 存在 Redis，可直接读出来——
   但必须**「读凭据 → 立刻发请求」写在同一个脚本里原子完成**，否则中间一次登出就会让凭据失效。
2. **让用户点一次**：最省事、最真实（走完整前端链路），代价是一次往返。
3. **等自然流量**：有定时任务或其它人正在用，直接采即可。

### 第 4 步：解析

```bash
# 总览：找候选热点
python scripts/arthas_profiler.py top ./wall_collapsed.txt --prefix com/yourcompany

# 调用树：看某个方法下面都干了什么
python scripts/arthas_profiler.py tree ./wall_collapsed.txt SomeService.doWork

# 祖先链：某个可疑方法到底挂在哪条链上、由谁触发
python scripts/arthas_profiler.py path ./wall_collapsed.txt SomeMapper.selectPage
```

`--threshold` 控制剪枝（默认忽略 <2 个采样点的节点）；`--unit` 是每个采样点代表多少毫秒（默认 10）。

### 第 5 步：出结论

结论必须包含：**关键路径树 → 各环节毫秒数与占比 → 定位到类+方法的根因 → 修复点与预期收益**。
只给「哪个方法慢」不算完 —— 要落到「这个循环里每条记录查了 4 次库」这种可直接改的粒度。

## 判读的四条硬规则

这几条是 wall 采样最容易读错的地方，**读错会把结论完全带偏**：

1. **IO 等待只能用 wall 采样。** `-e cpu` 看不到等数据库、等 socket 的时间。慢接口十有八九卡在 IO，
   默认就用 `-e wall`。wall 的单位是「墙钟毫秒」，不是 CPU 时间。
2. **collapsed 数据是「所有线程」的合计。** `top` 里的总数可能是几百秒，比你的请求大两个数量级 ——
   那是全 JVM 线程的总和。**必须用 `tree <某个根方法>` 切开，只看目标链路。**
3. **区分「请求线程在等」和「工作线程在干」。** 典型陷阱：请求线程 `CompletableFuture.join()` 阻塞 1330ms，
   同时线程池里的工作线程干了 1330ms 的活。两者在同一份采样里各出现一次，**不能相加**。
   做法：从**工作线程的根方法**（如 `XxxService.publishSingleChannel`）往下解析，那才是真实开销；
   请求线程那条栈只用来确认「总时长」和「有没有被其它环节拖住」。
4. **采样点数 × 采样间隔 ≈ 耗时。** 默认 10ms 一点，所以 `1 点 = 10ms`。
   别把「采样点数」当毫秒直接读。

另外：`@Async` 方法、定时任务线程、连接池预热线程都会出现在采样里。
用 `path` 回溯祖先链 —— 如果祖先链里出现 `AsyncExecutionInterceptor` / `FutureTask.run`，说明它是**并行**的，
通常**不在关键路径**上，别把它算进耗时。

## 常见坑

| 现象 | 原因 / 处理 |
|---|---|
| 脚本读 Arthas 输出一直卡住 | 用的 telnet 3658，IAC 协商没谈通。改走 HTTP API 8563 |
| `Affect(class count: 0)` | 增强已被重置（`-n` 用满或应用重启过）。先 `reset <类名>` 再 `trace` |
| 命令报 `#: command not found` | 把带 `#` 的行一起粘进 arthas 了。arthas 控制台不接受注释行 |
| arthas 里 `grep` 用不了 | arthas 的 grep 只作用于管道。要搜日志请回到 shell 里 grep |
| `async_exec` 报 `'sessionId' is required` | HTTP API 的异步作业要先 `init_session` 拿 sessionId，`pull_results` 还要 consumerId。多任务并发很麻烦，一般不值得 |
| `pull_results` 报 `'consumerId' is required` | 同上 |
| Druid 监控页 `/druid/sql.json` 返回 401/403 | 被应用的登录过滤器或权限过滤器挡了。用 JWT 能过登录，但权限过滤通常在应用层，绕不过 |
| 想统计 SQL 次数但拿不到 Druid | 退而求其次：用 `monitor` 统计 **Mapper/Service 方法**的调用次数，同理能定位 N+1 |

命令速查、HTTP API 细节与更多报错对照见 `references/arthas-commands.md`。

## 优先排查的常见反模式

拿到耗时排行后，先把候选热点往这四类上套 —— 传统 Java 单体里绝大多数「莫名慢」都落在其中：

| 反模式 | 采样中的典型形态 | 改法 |
|---|---|---|
| **循环内查库（N+1）** | `stream().map()` / `for` 里出现 `getOne` / `selectById` / `getById` | 先收集 id 批量查，再在内存里分组匹配 |
| **该缓存不缓存** | 入参极少、结果稳定，却每次都查库（字典、站点/模板配置等） | 加应用级缓存，写操作时失效 |
| **只读查询开事务** | 采样里出现 `TransactionInterceptor → AbstractPlatformTransactionManager.commit → setAutoCommit → socket 读` | 只读方法去掉 `@Transactional`，白省两次网络往返 |
| **循环里反复拉全量** | 每轮都 `list()` 整表，只为取其中一条 | 缓存全量结果，或提到循环外只取一次 |

**判定依据不是「看起来像」，而是采样里它实际占了多少毫秒。** 先用 `tree` 拿到占比，再对着代码改。

## 安全与凭据

- **本流程只读。** 不要用 `sh` 子命令去执行写数据、改配置、重启进程的命令。
- 凭据走环境变量或 `--env-file`，**不要硬编码进脚本、不要提交进仓库**。
- 凭据文件用完**立即删除**；如果密码曾在对话、截图、日志里出现过，提醒用户更换。
- 采样会产生少量磁盘文件（默认 `/tmp/`），诊断结束后可顺手清掉。

## 脚本

| 文件 | 说明 |
|---|---|
| `scripts/arthas_profiler.py` | 全部功能的单一入口：`probe` / `attach` / `sh` / `art` / `prof` / `sample` / `fetch` / `top` / `tree` / `path` |
| `references/arthas-commands.md` | arthas 命令速查、HTTP API 用法、报错对照表 |

脚本配置可用参数或环境变量：`ARTHAS_SSH_HOST` / `ARTHAS_SSH_PORT` / `ARTHAS_SSH_USER` /
`ARTHAS_SSH_PASSWORD` / `ARTHAS_API` / `ARTHAS_BOOT_JAR`。
