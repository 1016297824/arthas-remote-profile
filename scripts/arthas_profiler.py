#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""arthas-remote-profile 工具箱

通过 SSH 进入目标容器/主机，用 Arthas 的 HTTP API 做在线性能采样与诊断，
全程不重启、不改代码、不注入 agent。

设计要点
--------
1. 走 Arthas HTTP API（默认 http://127.0.0.1:8563/api）而不是 telnet 3658。
   telnet 有 IAC 选项协商，脚本化时容易读不到提示符而卡死。
2. 命令用 base64 包一层再下发，彻底回避多层 shell 的引号转义问题。
3. 采样事件默认 wall（墙钟）。IO/DB 等待在 cpu 采样里看不出来，而慢接口
   十有八九卡在等数据库。
4. collapsed 采样是**所有线程**的总和，解码时要指定正确的「工作线程」根方法，
   否则会把「请求线程在等」和「工作线程在干」重复计一遍。

用法
----
    export ARTHAS_SSH_HOST=localhost ARTHAS_SSH_PORT=22 ARTHAS_SSH_USER=root
    export ARTHAS_SSH_PASSWORD='***'

    python arthas_profiler.py probe
    python arthas_profiler.py attach --pid 12345 --jar /opt/arthas/arthas-boot.jar
    python arthas_profiler.py art "trace com.foo.Bar baz -n 5"
    python arthas_profiler.py sample --trigger "curl -s -X POST http://127.0.0.1:8080/api/do" --out ./wall.txt
    python arthas_profiler.py analyze top  ./wall.txt
    python arthas_profiler.py analyze tree ./wall.txt BaseTemplateDirective.execute
    python arthas_profiler.py analyze path ./wall.txt SomeServiceImpl.slowMethod

安全
----
本工具只读：只做采样、trace、monitor。不要用它执行会写数据、改配置、
重启进程的命令。凭据请走环境变量或 --env-file，不要写进代码或提交到仓库。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
from collections import defaultdict

try:
    import paramiko
except ImportError:  # ssh 相关子命令需要
    paramiko = None

DEFAULT_API = "http://127.0.0.1:8563/api"
CALL_SH = "/tmp/arthas_call.sh"
REMOTE_CMD = "/tmp/arthas_cmd.b64"
PROFILER_OUT = "/tmp/arthas_profile.out"


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
class Cfg:
    def __init__(self, a):
        self.host = a.host or os.environ.get("ARTHAS_SSH_HOST", "localhost")
        self.port = int(a.port or os.environ.get("ARTHAS_SSH_PORT", "22"))
        self.user = a.user or os.environ.get("ARTHAS_SSH_USER", "root")
        self.password = (a.password
                         or os.environ.get("ARTHAS_SSH_PASSWORD")
                         or os.environ.get("ARTHAS_PW"))
        self.api = a.api or os.environ.get("ARTHAS_API", DEFAULT_API)
        self.boot_jar = a.boot_jar or os.environ.get(
            "ARTHAS_BOOT_JAR", "/opt/arthas/arthas-boot.jar")
        self.timeout = a.timeout


def load_env_file(path):
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def connect(cfg):
    if paramiko is None:
        sys.exit("ERROR: 需要 paramiko（pip install paramiko）")
    if not cfg.password:
        sys.exit("ERROR: 缺少密码，请设 ARTHAS_SSH_PASSWORD 或用 --password/--env-file")
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(hostname=cfg.host, port=cfg.port, username=cfg.user,
              password=cfg.password, timeout=20,
              allow_agent=False, look_for_keys=False)
    return c


def run(client, cmd, timeout=300):
    _in, out, err = client.exec_command(cmd, timeout=timeout)
    return (out.read().decode("utf-8", "replace"),
            err.read().decode("utf-8", "replace"))


def sh_ok(client, cmd, timeout=60):
    out, _ = run(client, cmd, timeout=timeout)
    return out


# --------------------------------------------------------------------------- #
# arthas 通道
# --------------------------------------------------------------------------- #
def bootstrap(client, cfg):
    """在目标机写一个 /tmp/arthas_call.sh，把 arthas HTTP API 包成一条命令。"""
    script = (
        "#!/bin/sh\n"
        "printf '{\"action\":\"exec\",\"command\":\"%s\"}' \"$1\" > /tmp/arthas_req.json\n"
        "curl -s -m 600 -X POST " + cfg.api + " -H 'Content-Type: application/json' "
        "--data @/tmp/arthas_req.json\n"
    )
    b64 = base64.b64encode(script.encode()).decode()
    out, _ = run(client, "echo %s | base64 -d > %s; chmod +x %s; echo OK"
                 % (b64, CALL_SH, CALL_SH))
    return out.strip() == "OK"


def art_raw(client, cfg, command, timeout=600):
    """执行一条 arthas 命令，返回原始 JSON 文本。"""
    b64 = base64.b64encode(command.encode()).decode()
    cmd = ("echo %s | base64 -d > %s; sh %s \"$(cat %s)\""
           % (b64, REMOTE_CMD, CALL_SH, REMOTE_CMD))
    out, _ = run(client, cmd, timeout=timeout)
    return out


def art_fmt(raw):
    try:
        d = json.loads(raw)
    except Exception:
        return raw
    body = d.get("body", {})
    results = body.get("results")
    if not results:
        return json.dumps(d, ensure_ascii=False, indent=2)
    parts = []
    for r in results:
        for k in ("jobId", "type", "statusCode"):
            r.pop(k, None)
        parts.append(json.dumps(r, ensure_ascii=False, indent=2))
    return "\n".join(parts)


def art(cfg, client, command, timeout=600, quiet=False):
    raw = art_raw(client, cfg, command, timeout=timeout)
    return raw if quiet else art_fmt(raw)


def api_alive(client, cfg):
    out = sh_ok(client, "curl -s -m 5 -X POST %s -H 'Content-Type: application/json' "
                        "-d '{\"action\":\"exec\",\"command\":\"version\"}'" % cfg.api)
    return "version" in out


# --------------------------------------------------------------------------- #
# 子命令实现
# --------------------------------------------------------------------------- #
def cmd_probe(client, cfg):
    print("===== [1] 目标机上的 java 进程 =====")
    print(sh_ok(client, "ps -eo pid,etime,args | grep '[j]ava' | head -20"))
    print("===== [2] arthas HTTP API (%s) =====" % cfg.api)
    out = sh_ok(client,
                "curl -s -m 5 -X POST %s -H 'Content-Type: application/json' "
                "-d '{\"action\":\"exec\",\"command\":\"version\"}'" % cfg.api)
    print(out.strip() or "(无响应 — arthas 可能未启动，试 attach)")
    print("===== [3] telnet 3658 =====")
    print(sh_ok(client, "bash -c '(echo > /dev/tcp/127.0.0.1/3658) "
                        "2>/dev/null && echo OPEN || echo CLOSED'").strip())
    print("===== [4] arthas-boot.jar 是否存在 =====")
    print(sh_ok(client, "ls -l %s 2>/dev/null || find / -maxdepth 5 "
                        "-name 'arthas-boot.jar' 2>/dev/null | head -5" % cfg.boot_jar))


def cmd_attach(client, cfg, pid, jar):
    jar = jar or cfg.boot_jar
    if not pid:
        out = sh_ok(client, "ps -eo pid,args | grep '[j]ava' | grep -v arthas | head -5")
        print("未指定 --pid，候选进程：\n" + out)
        return
    print("附着 arthas 到 PID %s（%s）..." % (pid, jar))
    run(client, "cd /tmp && nohup java -jar %s %s > /tmp/arthas-boot.log 2>&1 &"
        % (jar, pid))
    for i in range(30):
        time.sleep(1)
        if api_alive(client, cfg):
            print("OK，HTTP API 已就绪（等待 %ds）" % (i + 1))
            return
    print("超时。日志：\n" + sh_ok(client, "tail -30 /tmp/arthas-boot.log"))


def cmd_prof(client, cfg, action, event, interval, fmt, remote_file):
    if action == "status":
        return print(art(cfg, client, "profiler status"))
    if action == "stop":
        cmd = "profiler stop"
        if remote_file:
            cmd += " --format %s --file %s" % (fmt, remote_file)
        return print(art(cfg, client, cmd))
    # start
    art(cfg, client, "profiler stop", quiet=True)
    cmd = "profiler start -e %s" % event
    if interval:
        cmd += " -i %s" % interval
    print(art(cfg, client, cmd))


def cmd_sample(client, cfg, trigger, event, remote_out, local_out, settle):
    """一步到位：重置采样 -> 执行触发命令 -> 停止采样 -> 下载 collapsed。"""
    script = (
        "sh %(call)s 'profiler stop' >/dev/null 2>&1\n"
        "echo '--- profiler start (%(event)s) ---'\n"
        "sh %(call)s 'profiler start -e %(event)s'\n"
        "echo '--- trigger ---'\n"
        "date '+%%H:%%M:%%S'; t0=$(date +%%s)\n"
        "%(trigger)s\n"
        "t1=$(date +%%s); echo \"trigger elapsed=$((t1-t0))s\"\n"
        "sleep %(settle)s\n"
        "echo '--- profiler stop ---'\n"
        "sh %(call)s 'profiler stop --format collapsed --file %(out)s'\n"
        "ls -l %(out)s 2>/dev/null\n"
    ) % dict(call=CALL_SH, event=event, trigger=trigger,
             settle=settle, out=remote_out)

    b64 = base64.b64encode(script.encode()).decode()
    out, err = run(client, "echo %s | base64 -d > /tmp/_sample.sh; bash /tmp/_sample.sh"
                   % b64, timeout=cfg.timeout)
    print(out)
    if err.strip():
        print("[stderr] " + err)
    if local_out:
        fetch(cfg, remote_out, local_out)


def fetch(cfg, remote, local):
    if paramiko is None:
        sys.exit("ERROR: 需要 paramiko")
    t = paramiko.Transport((cfg.host, cfg.port))
    t.connect(username=cfg.user, password=cfg.password)
    s = paramiko.SFTPClient.from_transport(t)
    s.get(remote, local)
    s.close()
    t.close()
    print("已下载 %s -> %s (%d bytes)" % (remote, local, os.path.getsize(local)))


# --------------------------------------------------------------------------- #
# collapsed 采样解析
# --------------------------------------------------------------------------- #
_LAMBDA = re.compile(r"\$\$Lambda\$\d+(/\d+)?")
_ENH = re.compile(r"\$\$EnhancerBySpringCGLIB\$\$[0-9a-f]+")
_FAST = re.compile(r"\$\$FastClassBySpringCGLIB\$\$[0-9a-f]+")
_PAREN = re.compile(r"\(.*\)$")

# 框架/AOP/JDBC 管道帧：折叠后业务调用直接挂在父节点下，树才看得清
NOISE = re.compile(
    r"(CglibAopProxy|MethodProxy|ReflectiveMethodInvocation|CglibMethodInvocation|"
    r"TransactionAspectSupport|TransactionInterceptor|"
    r"AsyncExecutionInterceptor|MethodInvocationProceedingJoinPoint|"
    r"AspectJ|JoinPoint|ProceedingJoinPoint|"
    r"UnifiedCall|Environment\.visit|Environment\.process|Template\.process|"
    r"^(java/sql|javax/sql|com/mysql|com/alibaba/druid|com/zaxxer/hikari)|"
    r"NativeProtocol|NativeSession|ClientPreparedStatement|ServerPreparedStatement|"
    r"(Packet|Header|Reader|InputStream)\.|FullReadInputStream|ReadAheadInputStream|"
    r"SocketInputStream|FilterInputStream|FilterChainImpl|FilterAdapter|"
    r"ConnectionProxyImpl|ConnectionImpl|ConnectionHolder|"
    r"NativeMethodAccessorImpl|DelegatingMethodAccessorImpl|GeneratedMethodAccessor|"
    r"Java_java_net|NET_Read|^recv$|^libc\.so|"
    r"StatementImpl|ResultSetImpl|PreparedStatement|"
    r"DefaultResultSetHandler|PreparedStatementHandler|SimpleExecutor|"
    r"MapperMethod|MapperProxy|CachingExecutor|BaseExecutor|"
    r"AbstractPlatformTransactionManager|DataSourceTransactionManager|"
    r"TransactionSynchronizationManager|DruidPooledConnection)"
)


def norm(f):
    f = _LAMBDA.sub("", f)
    f = _ENH.sub("", f)
    f = _FAST.sub("", f)
    return _PAREN.sub("", f)


class Node:
    __slots__ = ("name", "total", "self_", "children")

    def __init__(self, name):
        self.name = name
        self.total = 0
        self.self_ = 0
        self.children = {}


def read_stacks(path):
    for line in open(path, "r", encoding="utf-8", errors="replace"):
        line = line.rstrip("\n")
        if not line.strip():
            continue
        stack, _, cnt = line.rpartition(" ")
        try:
            cnt = int(cnt)
        except ValueError:
            continue
        yield [norm(x) for x in stack.split(";") if x], cnt


def build(path, drop_noise=True):
    root = Node("(root)")
    for frames, cnt in read_stacks(path):
        if drop_noise:
            frames = [f for f in frames if not NOISE.search(f)]
        root.total += cnt
        node = root
        for i, fr in enumerate(frames):
            ch = node.children.get(fr)
            if ch is None:
                ch = Node(fr)
                node.children[fr] = ch
            ch.total += cnt
            if i == len(frames) - 1:
                ch.self_ += cnt
            node = ch
    return root


def walk(n, out):
    out.append(n)
    for c in n.children.values():
        walk(c, out)


def find(root, suffix):
    hits = []

    def dfs(n):
        if n.name.endswith(suffix):
            hits.append(n)
        for c in n.children.values():
            dfs(c)

    dfs(root)
    return max(hits, key=lambda x: x.total) if hits else None


def fmt_ms(samples, unit):
    return "%.0f ms" % (samples * unit)


def cmd_top(path, unit, prefix, keep_noise):
    root = build(path, drop_noise=not keep_noise)
    nodes = []
    walk(root, nodes)
    print("总采样 %d 点 ≈ %.0f ms wall（@%gms/点，注意这是所有线程的合计）"
          % (root.total, root.total * unit, unit))
    filt = tuple(prefix)
    cand = [n for n in nodes if not filt or n.name.startswith(filt)]
    print("\n===== TOP 30 inclusive =====")
    for n in sorted(cand, key=lambda x: -x.total)[:30]:
        print("%7d 点  %9.0f ms  self=%-6d  %s"
              % (n.total, n.total * unit, n.self_, n.name))


def cmd_tree(path, suffix, thr, unit, keep_noise):
    root = build(path, drop_noise=not keep_noise)
    n = find(root, suffix)
    if not n:
        return print("未找到: " + suffix)
    print("根=%s  总计=%s   阈值=%d 点" % (suffix, fmt_ms(n.total, unit), thr))

    def dump(node, depth):
        if node.total < thr or depth > 40:
            return
        print("  " * depth + "%s  tot=%s self=%s"
              % (node.name.split("/")[-1], fmt_ms(node.total, unit),
                 fmt_ms(node.self_, unit)))
        for c in sorted(node.children.values(), key=lambda x: -x.total):
            dump(c, depth + 1)

    dump(n, 0)


def cmd_path(path, suffix, depth, unit):
    agg = defaultdict(int)
    for frames, cnt in read_stacks(path):
        for i, f in enumerate(frames):
            if f.endswith(suffix):
                agg[tuple(frames[max(0, i - depth):i + 1])] += cnt
    if not agg:
        return print("未找到: " + suffix)
    for chain, c in sorted(agg.items(), key=lambda x: -x[1]):
        print("\n[%d 点 = %s]" % (c, fmt_ms(c, unit)))
        for i, f in enumerate(chain):
            print("   " * i + "  > " + f.split("/")[-1])


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(
        description="SSH + Arthas 远程 JVM 性能诊断工具箱",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host")
    ap.add_argument("--port")
    ap.add_argument("--user")
    ap.add_argument("--password")
    ap.add_argument("--api", help="arthas HTTP API 地址（容器内可达）")
    ap.add_argument("--boot-jar", help="arthas-boot.jar 路径")
    ap.add_argument("--env-file", help="加载 KEY=VALUE 凭据文件")
    ap.add_argument("--timeout", type=int, default=300)

    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("probe", help="探测目标机状态")
    p = sub.add_parser("attach", help="启动 arthas 并附着到目标 JVM")
    p.add_argument("--pid")
    p.add_argument("--jar")

    p = sub.add_parser("sh", help="目标机执行 shell")
    p.add_argument("command")

    p = sub.add_parser("art", help="执行 arthas 命令（trace/watch/monitor/ognl...）")
    p.add_argument("command")

    p = sub.add_parser("prof", help="profiler 控制")
    p.add_argument("action", choices=["start", "stop", "status"])
    p.add_argument("--event", default="wall", choices=["wall", "cpu", "alloc", "lock"])
    p.add_argument("--interval", default=None, help="采样间隔，如 10ms")
    p.add_argument("--format", default="collapsed",
                   choices=["collapsed", "html", "flamegraph", "tree", "jfr"])
    p.add_argument("--file", dest="remote_file", default=None)

    p = sub.add_parser("sample", help="一步完成：起采样 -> 触发 -> 停采样 -> 下载")
    p.add_argument("--trigger", required=True, help="触发一次业务的 shell 命令")
    p.add_argument("--event", default="wall")
    p.add_argument("--out", default=None, help="下载到本地的路径")
    p.add_argument("--remote-out", default=PROFILER_OUT)
    p.add_argument("--settle", type=int, default=1, help="停采样前等待秒数")

    p = sub.add_parser("fetch", help="下载目标机文件")
    p.add_argument("remote")
    p.add_argument("local")

    # ---- 以下三个是纯本地分析，不连 SSH ----
    p = sub.add_parser("top", help="采样总览 + 热点聚合")
    p.add_argument("file")
    p.add_argument("--unit", type=float, default=10.0, help="每个采样点代表多少毫秒")
    p.add_argument("--prefix", action="append", default=[], help="只看这些包前缀，可重复")
    p.add_argument("--keep-noise", action="store_true", help="保留框架/AOP/JDBC 管道帧")

    p = sub.add_parser("tree", help="打印某方法之下的调用树")
    p.add_argument("file")
    p.add_argument("method", help="方法后缀，如 BaseTemplateDirective.execute")
    p.add_argument("--threshold", type=int, default=2, help="忽略小于该采样点数的节点")
    p.add_argument("--unit", type=float, default=10.0)
    p.add_argument("--keep-noise", action="store_true")

    p = sub.add_parser("path", help="回溯某方法的祖先链，定位它挂在谁下面")
    p.add_argument("file")
    p.add_argument("method", help="方法后缀")
    p.add_argument("--depth", type=int, default=6, help="向上回溯层数")
    p.add_argument("--unit", type=float, default=10.0)

    a = ap.parse_args()
    if a.env_file:
        load_env_file(a.env_file)
    cfg = Cfg(a)

    # ---- 纯本地分析，不需要 SSH ----
    if a.cmd in ("top", "tree", "path"):
        if a.cmd == "top":
            return cmd_top(a.file, a.unit, a.prefix, a.keep_noise)
        if a.cmd == "tree":
            return cmd_tree(a.file, a.method, a.threshold, a.unit, a.keep_noise)
        return cmd_path(a.file, a.method, a.depth, a.unit)

    client = connect(cfg)
    try:
        if a.cmd != "attach":
            bootstrap(client, cfg)
        if a.cmd == "probe":
            cmd_probe(client, cfg)
        elif a.cmd == "attach":
            cmd_attach(client, cfg, a.pid, a.jar)
        elif a.cmd == "sh":
            out, err = run(client, a.command, timeout=cfg.timeout)
            print(out, end="")
            if err.strip():
                print("[stderr] " + err, file=sys.stderr)
        elif a.cmd == "art":
            print(art(cfg, client, a.command, timeout=cfg.timeout))
        elif a.cmd == "prof":
            cmd_prof(client, cfg, a.action, a.event, a.interval, a.format, a.remote_file)
        elif a.cmd == "sample":
            cmd_sample(client, cfg, a.trigger, a.event, a.remote_out, a.out, a.settle)
        elif a.cmd == "fetch":
            fetch(cfg, a.remote, a.local)
    finally:
        client.close()


if __name__ == "__main__":
    main()
