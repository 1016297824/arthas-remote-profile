# Arthas 命令与 HTTP API 速查

## 一、附着与端口

```bash
# 附着到指定 PID（后台常驻，随后即可用 HTTP API / telnet）
nohup java -jar /opt/arthas/arthas-boot.jar <PID> > ${ARTHAS_REMOTE_TMP:-/tmp}/arthas-boot.log 2>&1 &

# 只看有哪些 java 进程
ps -eo pid,etime,args | grep '[j]ava'
```

| 端口 | 用途 | 脚本化友好度 |
|---|---|---|
| 3658 | telnet 交互控制台 | **差** —— 有 IAC 选项协商，Python/expect 常说不上话就假死 |
| 8563 | HTTP API | **好** —— 一个 POST 就是一条命令 |

## 二、HTTP API

```
POST http://127.0.0.1:8563/api
Content-Type: application/json
```

### 同步执行一条命令

```json
{"action":"exec","command":"trace com.foo.Bar baz -n 5"}
```

响应结构（`body.results[0].executeResult` 或各类命令专属字段里是真正的输出）：

```json
{
  "body": {
    "command": "trace com.foo.Bar baz -n 5",
    "jobId": 24,
    "jobStatus": "TERMINATED",
    "results": [ {"jobId": 24, "type": "trace", "..." : "..."},
                 {"jobId": 24, "statusCode": 0, "type": "status"} ]
  },
  "sessionId": "c98f1c11-...",
  "state": "SUCCEEDED"
}
```

**长命令会阻塞到这个 HTTP 请求超时为止。** `monitor` / 不带 `-n` 的 `trace` 是流式命令，
永远不会自然结束 —— 这也是不建议用 HTTP API 跑它们的根本原因。
`profiler start` / `profiler stop` 是短命令，立即返回，非常适合脚本化。

### 异步作业（能用但不推荐）

```json
{"action":"init_session"}
{"action":"async_exec","sessionId":"<上一步返回的>","command":"monitor -c 5 com.foo.Bar baz"}
{"action":"pull_results","sessionId":"<sid>","jobId":33,"consumerId":"<??>"}
{"action":"interrupt_job","sessionId":"<sid>","jobId":33}
```

坑：`pull_results` 除了 `sessionId` 还要 `consumerId`，而且一个 session 同时只跑得动一个 job
（并发起多个 `async_exec`，只有第一个返回 jobId）。投入产出比很低，**除非确实需要流式命令的中间结果，否则别用**。

### 引号问题

命令里带引号、`$`、换行时，逐层 shell 转义极易出错。稳妥做法：
本地 base64 编码 → 远端 `base64 -d` 落盘 → 再作为参数传给封装脚本。

```bash
# 远端封装脚本（一次性生成）
cat > ${ARTHAS_REMOTE_TMP:-/tmp}/arthas_call.sh <<'EOS'
#!/bin/sh
printf '{"action":"exec","command":"%s"}' "$1" > ${ARTHAS_REMOTE_TMP:-/tmp}/arthas_req.json
curl -s -m 600 -X POST http://127.0.0.1:8563/api \
  -H 'Content-Type: application/json' --data @${ARTHAS_REMOTE_TMP:-/tmp}/arthas_req.json
EOS
chmod +x ${ARTHAS_REMOTE_TMP:-/tmp}/arthas_call.sh

# 调用（命令经 base64 传递，零转义风险）
echo '<base64>' | base64 -d > ${ARTHAS_REMOTE_TMP:-/tmp}/cmd.txt
sh ${ARTHAS_REMOTE_TMP:-/tmp}/arthas_call.sh "$(cat ${ARTHAS_REMOTE_TMP:-/tmp}/cmd.txt)"
```

## 三、命令速查

### 先看全局

| 命令 | 作用 |
|---|---|
| `dashboard` | 全局概览：线程、内存、GC、热点线程 |
| `thread -n 5` | CPU 占用最高的 5 个线程 |
| `thread -b` | 找出正在阻塞其它线程的锁持有者 |
| `jvm` | JVM 参数、启动信息 |

### 看调用链耗时

| 命令 | 作用 |
|---|---|
| `trace <类> <方法> -n 5` | 打印 5 次调用的调用链与每层耗时 **（最常用）** |
| `trace <类> <方法> -n 5 --skipJDKMethod false` | 保留 JDK 内部帧 |
| `trace <类> <方法> -n 5 -c 'params[0]>10'` | 带条件，只看特定入参 |
| `stack <类> <方法> -n 5` | 只打印调用栈，不看耗时 |

### 看调用次数与入参

| 命令 | 作用 |
|---|---|
| `monitor -c 5 <类> <方法>` | 按周期滚动输出 `total / success / fail / avg-rt / max-rt`。**数调用次数用它** |
| `watch <类> <方法> '{params, returnObj}' -x 3 -n 5` | 观察入参和返回值 |
| `tt -t <类> <方法> -n 100` | 记录 100 次调用现场，之后可 `tt -i <index>` 回放 |

> `monitor` 是流式命令，HTTP API 下会一直阻塞。要用它就得接受「起一个长连接 / 用 telnet 手工看」，
> 或者退到应用层统计（见下）。

### 火焰图 / 采样

| 命令 | 作用 |
|---|---|
| `profiler start -e wall -i 10ms` | 开墙钟采样，10ms 一点 |
| `profiler start -e cpu` | 开 CPU 采样（看不到 IO 等待） |
| `profiler status` | 采样跑了多久 |
| `profiler stop --format collapsed --file ${ARTHAS_REMOTE_TMP:-/tmp}/x.txt` | 输出折叠栈文本（**最好解析**） |
| `profiler stop --format html --file ${ARTHAS_REMOTE_TMP:-/tmp}/x.html` | 输出交互式火焰图（给人看） |
| `profiler stop --format flamegraph` | 输出 SVG 火焰图 |

**collapsed 格式**：每行 `frame;frame;frame <采样点数>`，帧名是 `com/foo/Bar.method` 形式，
最后一个是该栈的采样次数。10ms 间隔时「1 点 = 10ms」。

不加 `--file` 时，输出落在 arthas 工作目录的 `arthas-output/` 下。

### 其它

| 命令 | 作用 |
|---|---|
| `sc -d <类全名>` | 查类从哪个 jar 加载的、类加载器是谁（**验证线上是不是新包很有用**） |
| `sm <类全名>` | 列出类的所有方法 |
| `jad <类全名>` | 反编译线上字节码（**确认改动是否真的生效**的终极手段） |
| `ognl '@com.foo.Bar@staticField'` | 读静态字段 / 调静态方法 |
| `reset <类全名>` | 清除该类的字节码增强（trace/monitor 之前若报异常先做这个） |
| `stop` | 退出 arthas，**不影响业务进程** |

## 四、报错对照

| 报错 | 原因 | 处理 |
|---|---|---|
| `Affect(class count: 0)` | 增强被重置 / 类名写错 / 方法名是继承来的 | 先 `reset <类>`；确认类名用全限定名；继承的方法要写声明类 |
| `Enhance error! class may be already enhanced` | 增强冲突 | `reset <类>` 后重试 |
| `#: command not found` | 把 `#` 注释行粘进了 arthas 控制台 | 去掉注释行 |
| `Profiling is already started` | 上次没停 | 先 `profiler stop` |
| `Profiling is not started` | 还没起就停 | 先 `profiler start` |
| arthas 里 `grep` 无输出 | arthas 的 grep 只用于管道 | 回 shell 里 grep 日志 |
| HTTP API 返回 `'sessionId' is required` | 用了 `async_exec` 但没先 `init_session` | 先 init_session |
| `/druid/sql.json` 返回 401 | 被应用自己的登录过滤器挡了 | 带上应用 JWT 再试 |
| `/druid/sql.json` 返回 403 `未授权该url` | 应用层的接口权限过滤 | 绕不过，改用 `monitor` 统计 Mapper/Service 调用次数 |

## 五、拿不到 Druid 时怎么数 SQL 次数

Druid 的 `sql.json` 能给出每条 SQL 的 `executeCount`，是最直接的「一次请求打了几条 SQL」。
被权限挡住时，退而求其次：

1. **`monitor` 统计 Mapper / Service 方法调用次数** —— 数量级等价，一样能锤死 N+1。
2. **看 MyBatis 的日志**：把 Mapper 包日志级别调到 DEBUG，一条请求的 SQL 条数一目了然。
3. **数据库侧 `performance_schema`**：
   ```sql
   SELECT DIGEST_TEXT, COUNT_STAR, SUM_TIMER_WAIT
   FROM performance_schema.events_statements_summary_by_digest
   ORDER BY COUNT_STAR DESC LIMIT 30;
   ```
   请求前后各取一次做差值，即可得到单次请求的 SQL 分布（只读，安全）。
