"""Android 工程本地研发任务入口（编译 / 安装 / 启动 / 日志）。

默认作用于 devtask.config.json 里的工程；也可用 `--project` 或环境变量 DEVTASK_PROJECT 覆盖。
用法见 README.md；命令实现集中在 COMMANDS 注册表，新增命令只需加一个 `cmd_` 函数。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable

FROZEN = bool(getattr(sys, "frozen", False))


def _tool_dir() -> Path:
    # PyInstaller 会把资源解压到只读的临时目录 _MEIPASS，配置和日志必须留在 exe 旁边
    if FROZEN:
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


TOOL_DIR = _tool_dir()
# 只作为完全识别不出包名时的兜底，正常情况从工程的 AndroidManifest.xml 读
DEFAULT_APP_ID = "com.mercedes.customize"
DEFAULT_LOG_TAG = "Customize_"
ANDROID_NS = "{http://schemas.android.com/apk/res/android}"
GRADLE_WRAPPER = "gradlew.bat" if os.name == "nt" else "./gradlew"
# pythonw 启动的 GUI 没有控制台，子进程默认会各自弹一个控制台窗口，必须显式禁止
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
# 未指定行数时回放的历史行数上限，避免整段缓冲一次灌进界面
HISTORY_LINES = 200

PROJECT_ROOT = TOOL_DIR
APPLICATION_ID = DEFAULT_APP_ID
LAUNCH_ACTIVITY = ""
LOG_TAG = DEFAULT_LOG_TAG
FLAVOR = ""


class TaskError(Exception):
    """可预期的失败：只打印提示，不打印 traceback。"""


class StopStreaming(Exception):
    """由 stream_logcat 的回调抛出，用于主动结束日志流。"""


def log(message: str) -> None:
    print(f"[devtask] {message}", flush=True)


def read_config(project: Path) -> dict:
    path = project / "devtask.config.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise TaskError(f"配置文件解析失败：{path}（{e}）") from e


def read_manifest_launcher(manifest: Path) -> tuple[str, str]:
    """从 AndroidManifest.xml 读出 (包名, launcher Activity 全限定名)。

    包名取自 manifest 的 package 属性（AGP 8 之后可能没有，退回 namespace）；
    launcher 是带 MAIN + LAUNCHER intent-filter 的 activity，名字按惯例补全成全限定名。
    """
    try:
        root = ET.parse(manifest).getroot()
    except (OSError, ET.ParseError):
        return "", ""
    package = root.get("package") or root.get(f"{ANDROID_NS}namespace") or ""
    launcher = ""
    for activity in root.iter("activity"):
        for f in activity.iter("intent-filter"):
            actions = {a.get(f"{ANDROID_NS}name") for a in f.findall("action")}
            categories = {c.get(f"{ANDROID_NS}name") for c in f.findall("category")}
            if "android.intent.action.MAIN" in actions and "android.intent.category.LAUNCHER" in categories:
                name = activity.get(f"{ANDROID_NS}name") or ""
                if name.startswith("."):
                    name = package + name
                launcher = launcher or name
    return package, launcher


def read_gradle_package(module: Path) -> str:
    """AGP 8 起 manifest 不再写 package，包名只能从 build.gradle 的 applicationId/namespace 取。"""
    for name in ("build.gradle", "build.gradle.kts"):
        path = module / name
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        match = re.search(r"""\b(?:applicationId|namespace)\b\s*[=\s]\s*["']([\w.]+)["']""", text)
        if match:
            return match.group(1)
    return ""


def detect_launcher(preferred_flavor: str | None = None) -> tuple[str, str]:
    """找出工程的包名与启动 Activity，用于安装后拉起主界面。

    优先看已合并的 manifest（flavor 的 activity 都已合入），再退回源码里的 main/flavor manifest；
    包名单独在 build.gradle 里（AGP 8）时也能取到。都识别不出时返回空串，由调用方提示配置。
    """
    merged_dir = PROJECT_ROOT / "app/build/intermediates/merged_manifest"
    dirs = [merged_dir, PROJECT_ROOT / "app/src"]
    manifests = [p for d in dirs for p in sorted(d.glob("*/AndroidManifest.xml"))]
    if preferred_flavor:
        # 同一工程可能有多个 flavor 的合并产物，优先看配置的那个
        manifests.sort(key=lambda p: preferred_flavor.lower() not in p.parent.name.lower())
    package = ""
    for manifest in manifests:
        found_package, launcher = read_manifest_launcher(manifest)
        package = package or found_package
        if launcher:
            return package or read_gradle_package(PROJECT_ROOT / "app"), launcher
    return read_gradle_package(PROJECT_ROOT / "app"), ""


def set_project(project: str | Path | None, *, quiet: bool = False) -> Path:
    """确定本次作用于哪个 Android 工程：参数 > 环境变量 > 配置文件 > 当前目录。

    工程根目录下可放 devtask.config.json 覆盖 package / launchActivity / logTag / flavor。
    包名与启动页默认从工程 manifest 识别，无需逐个工程手写。
    """
    candidates = [project, os.environ.get("DEVTASK_PROJECT"), read_config(TOOL_DIR).get("project"), Path.cwd()]
    chosen = next((Path(c) for c in candidates if c and (Path(c) / GRADLE_WRAPPER).exists()), None)
    if chosen is None:
        raise TaskError(
            f"找不到 Android 工程（需要在含 {GRADLE_WRAPPER} 的目录）：{candidates[0] or '未指定'}。"
            "用 --project <路径>、环境变量 DEVTASK_PROJECT，或在 devtask.config.json 里配置 project。"
        )
    global PROJECT_ROOT, APPLICATION_ID, LAUNCH_ACTIVITY, LOG_TAG, FLAVOR
    PROJECT_ROOT = chosen.resolve()
    cfg = read_config(PROJECT_ROOT)
    FLAVOR = cfg.get("flavor", "")
    detected_package, detected_launcher = detect_launcher(FLAVOR)
    APPLICATION_ID = cfg.get("package") or detected_package or DEFAULT_APP_ID
    LAUNCH_ACTIVITY = cfg.get("launchActivity") or detected_launcher
    if LAUNCH_ACTIVITY.startswith("."):
        LAUNCH_ACTIVITY = APPLICATION_ID + LAUNCH_ACTIVITY
    LOG_TAG = cfg.get("logTag", DEFAULT_LOG_TAG)
    if not quiet:
        log(f"工程：{PROJECT_ROOT}（{APPLICATION_ID}）")
    return PROJECT_ROOT


def run(cmd: list[str], *, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    """执行子进程并透传输出；失败时按需抛 TaskError，调用方也可传 check=False 自行判定。

    Windows 上 gradlew.bat 不是可执行文件，subprocess 不会自己走 cmd 解析，必须 shell=True。
    """
    log("$ " + " ".join(cmd))
    proc = subprocess.run(
        cmd, cwd=str(cwd or PROJECT_ROOT), creationflags=NO_WINDOW, shell=cmd[0] == GRADLE_WRAPPER
    )
    if check and proc.returncode != 0:
        raise TaskError(f"命令失败（退出码 {proc.returncode}）：{' '.join(cmd)}")
    return proc


def capture(cmd: list[str]) -> tuple[int, str]:
    proc = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def find_adb() -> str:
    """优先用 local.properties 里的 SDK，避免 PATH 上的旧版 adb 与车机协议不匹配。"""
    sdk = None
    props = PROJECT_ROOT / "local.properties"
    if props.exists():
        for line in props.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("sdk.dir="):
                sdk = line.split("=", 1)[1].strip().replace("\\:", ":")
                break
    if sdk:
        candidate = Path(sdk) / "platform-tools" / ("adb.exe" if os.name == "nt" else "adb")
        if candidate.exists():
            return str(candidate)
    adb = shutil.which("adb")
    if not adb:
        raise TaskError("找不到 adb，请确认 Android SDK platform-tools 已安装")
    return adb


def connected_devices(adb: str) -> list[str]:
    code, out = capture([adb, "devices"])
    if code != 0:
        raise TaskError(f"adb devices 执行失败：{out.strip()}")
    devices = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            devices.append(parts[0])
    return devices


def pick_device(adb: str, serial: str | None) -> str:
    devices = connected_devices(adb)
    if serial:
        if serial not in devices:
            raise TaskError(f"设备 {serial} 未连接或未授权，当前可用：{devices or '无'}")
        return serial
    if not devices:
        raise TaskError(
            "未检测到已授权设备。请车机开启 USB 调试并确认授权弹窗；"
            "只想验证编译可用 --no-install。"
        )
    if len(devices) > 1:
        raise TaskError(f"检测到多台设备 {devices}，请用 --device 指定")
    return devices[0]


def variants() -> dict[str, Path]:
    """从 AGP 的 output-metadata.json 读出所有变体（键为变体名）及其 APK 路径。

    这是 AGP 自己发布的产物清单，比手拼路径可靠；androidTest 等不带 variantName 的目录自然被跳过。
    """
    pattern = "*/build/outputs/apk/*/*/output-metadata.json"
    found: dict[str, Path] = {}
    for meta in sorted(PROJECT_ROOT.glob(pattern)):
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for element in data.get("elements", []):
            output = element.get("outputFile")
            if data.get("variantName") and output:
                found[data["variantName"]] = meta.parent / output
    return found


BUILD_TYPES = ("Debug", "Release", "BenchmarkRelease", "NonMinifiedRelease")


def build_type_of(variant: str) -> str | None:
    return next((s for s in BUILD_TYPES if variant.endswith(s)), None)


def flavor_names(variants: dict[str, Path]) -> list[str]:
    """从变体名里剥出 flavor：productADebug -> productA。

    build type 有长有短（Release 是 BenchmarkRelease 的后缀），长后缀先命中；
    剥完若仍以另一个已知 flavor 开头（productABenchmark = productA + Benchmark），取更短的那个。
    """
    suffixes = sorted(BUILD_TYPES, key=len, reverse=True)
    raw: list[str] = []
    for variant in variants:
        suffix = build_type_of(variant)
        if suffix:
            raw.append(variant[: -len(suffix)])
    names: list[str] = []
    for name in raw:
        shorter = next((n for n in raw if n != name and name.startswith(n)), name)
        if shorter and shorter not in names:
            names.append(shorter)
    return names


def find_variant(name: str, available: dict[str, Path]) -> str | None:
    # 元数据里是 productADebug，Gradle 任务名却是 assembleProductADebug，只能忽略大小写比对
    return next((v for v in available if v.lower() == name.lower()), None)


def variant_name(build_type: str, flavor: str) -> str:
    return f"{flavor}{build_type.capitalize()}"


def variants_of(build_type: str) -> dict[str, Path]:
    return {v: p for v, p in variants().items() if v.endswith(build_type.capitalize())}


def resolve_variant(build_type: str, flavor: str | None) -> tuple[str, Path]:
    """确定要构建的变体：显式 --flavor > 工程配置 > 唯一候选；歧义时报错而不是猜。

    不带 flavor 的工程只有 Debug/Release 两个变体，行为与以前一致。
    """
    wanted = flavor or FLAVOR or None
    all_variants = variants()
    suffix = build_type.capitalize()
    if wanted:
        variant = find_variant(f"{wanted}{suffix}", all_variants)
        if variant is None:
            raise TaskError(
                f"变体 {wanted}{suffix} 不存在，可用的 {build_type} 变体：{list(variants_of(build_type)) or '无'}"
            )
        return variant, all_variants[variant]

    candidates = variants_of(build_type)
    flavors = flavor_names(variants())
    if len(candidates) == 1:
        return next(iter(candidates.items()))
    if len(flavors) > 1:
        raise TaskError(
            f"工程有多个 flavor {flavors}，需用 --flavor 指定，"
            f"或在该工程 devtask.config.json 里配置 \"flavor\"。"
        )
    # 无法从产物反推（例如从未构建过）：交给 Gradle 的默认变体，绝不带 flavor
    return build_type.capitalize(), apk_path(build_type)


def apk_path(build_type: str, flavor: str | None = None) -> Path:
    folder = PROJECT_ROOT / "app" / "build" / "outputs" / "apk"
    if flavor:
        found = find_variant(variant_name(build_type, flavor), variants())
        if found:
            return variants()[found]
        return folder / flavor / build_type / f"app-{flavor}-{build_type}.apk"
    return folder / build_type / f"app-{build_type}.apk"


def gradle_assemble(args: argparse.Namespace) -> Path:
    variant, apk = resolve_variant(args.build_type, args.flavor)
    # 元数据是 productADebug，Gradle 任务名对应 assembleProductADebug
    task = f":app:assemble{variant[:1].upper()}{variant[1:]}"
    run([GRADLE_WRAPPER, task])
    if not apk.exists():
        raise TaskError(
            f"构建成功但未找到产物：{apk}。"
            "变量多时用 --flavor 指定，或确认产物路径（app/build/outputs/apk）"
        )
    size_mb = apk.stat().st_size / 1024 / 1024
    log(f"产物：{apk}（{size_mb:.1f} MB）")
    return apk


def install_apk(apk: Path, serial: str) -> None:
    adb = find_adb()
    code, out = capture([adb, "-s", serial, "install", "-r", "-t", str(apk)])
    if code == 0 and "Success" in out:
        log(f"安装成功：{serial}")
        return
    # 车机常因 /data 分区或签名校验导致直装失败，降级为流式安装
    log(f"常规安装失败，改用流式安装：{out.strip()}")
    with open(apk, "rb") as f:
        log(f"$ adb -s {serial} install -r -t -S {apk.stat().st_size}")
        proc = subprocess.run(
            [adb, "-s", serial, "install", "-r", "-t", "-S", str(apk.stat().st_size)],
            stdin=f,
            creationflags=NO_WINDOW,
        )
    if proc.returncode != 0:
        raise TaskError("安装失败，请检查车机是否开启了安装拦截或签名不一致（需先卸载同包名旧应用）")
    log(f"安装成功：{serial}")


def launch_app(serial: str) -> None:
    if not LAUNCH_ACTIVITY:
        raise TaskError(
            "没识别到启动 Activity（工程需要构建过一次，或 manifest 里没有 MAIN+LAUNCHER 的 activity）；"
            "可在工程 devtask.config.json 里配置 launchActivity，或加 --no-launch 跳过启动。"
        )
    adb = find_adb()
    component = f"{APPLICATION_ID}/{LAUNCH_ACTIVITY}"
    run([adb, "-s", serial, "shell", "am", "start", "-n", component])
    log(f"已启动 {component}")


def cmd_apk(args: argparse.Namespace) -> int:
    started = time.time()
    apk = gradle_assemble(args)
    if args.no_install:
        log("跳过安装（--no-install）")
        return 0
    adb = find_adb()
    serial = pick_device(adb, args.device)
    if args.fresh:
        run([adb, "-s", serial, "uninstall", APPLICATION_ID], check=False)
    install_apk(apk, serial)
    if not args.no_launch:
        launch_app(serial)
    log(f"完成，耗时 {time.time() - started:.1f}s")
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    gradle_assemble(args)
    return 0


def app_pid(adb: str, serial: str) -> str | None:
    _, out = capture([adb, "-s", serial, "shell", "pidof", "-s", APPLICATION_ID])
    pid = out.strip()
    return pid if pid.isdigit() else None


def logcat_cmd(adb: str, serial: str, pid: str | None) -> list[str]:
    """构造日志命令。

    设备侧过滤只保留 `--pid`：`logcat -s` 只做 TAG 精确匹配（`Customize_*` 的 `*` 是字面量），
    而本机车机上的 `--regex` 实测完全不生效，所以关键词过滤放在主机侧做。

    注意 `--pid` 与 `-t`/`-T` 组合会让本车机的 logcat 丢掉跟随模式：打印完缓冲就 EOF 退出。
    所以回放历史必须用带 `-d` 的独立命令（见 `dump_logcat`），跟随流只用于取新日志。
    """
    cmd = [adb, "-s", serial, "logcat", "-v", "time"]
    if pid:
        cmd.append(f"--pid={pid}")
    return cmd


def dump_logcat(adb: str, serial: str, pid: str | None, since: str | None, tail: int | None) -> str:
    """一次性回放历史日志（`-d` 读完即退出）。`since` 与 `tail` 只用于缩小范围。"""
    cmd = logcat_cmd(adb, serial, pid)
    if since:
        # 先按时间点截断，再按行数兜底：缓冲里同一时刻可能就堆积了上万行
        cmd += ["-T", since, "-t", str(tail or HISTORY_LINES)]
    else:
        cmd += ["-t", str(tail or HISTORY_LINES)]
    cmd.append("-d")
    _, out = capture(cmd)
    return out


def device_time(adb: str, serial: str) -> str:
    """车机本地时间，格式与 `logcat -T` 要求一致。"""
    _, out = capture([adb, "-s", serial, "shell", "date", "+%m-%d\\ %H:%M:%S.000"])
    value = out.strip()
    if not re.match(r"\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}", value):
        raise TaskError(f"无法读取车机时间：{value or '空输出'}")
    return value


def stream_logcat(
    adb: str,
    serial: str,
    tags: list[str] | None,
    pid: str | None,
    on_line: Callable[[str], None],
    *,
    since_now: bool = False,
    tail: int | None = None,
    on_start: Callable[[subprocess.Popen], None] | None = None,
    watch_app: bool = False,
    on_line_stopped: Callable[[], bool] | None = None,
) -> None:
    """把日志逐行回调出去，供命令行与 GUI 共用。

    `tags` 为 None 表示不按关键词过滤；`since_now` 只取本次连接之后的日志；
    `tail` 在启动时先回放最近 N 行历史（独立的 `-d` 命令，读完即退出）。
    `on_start` 拿到实时 logcat 进程句柄，供外部提前终止；
    `watch_app` 时 pid 会随应用重启自动重连（重装/重启后进程号会变），
    `on_line_stopped` 用于区分"被外部终止"与"应用重启"。
    `on_line` 抛出 StopStreaming 即可停止读取（GUI 用它实现"停止日志"）。
    """
    since = device_time(adb, serial) if since_now else None
    if tail:
        if watch_app:
            pid = _wait_app_pid(adb, serial)
        history = dump_logcat(adb, serial, pid, since, tail)
        for line in history.splitlines(keepends=True):
            if tags and not any(tag in line for tag in tags):
                continue
            on_line(line)
    while True:
        if watch_app:
            pid = _wait_app_pid(adb, serial)
        _stream_once(adb, serial, tags, pid, on_line, on_start=on_start)
        # 调用方主动停止时也会走到这里（读端被关闭），不能误判为应用重启
        stopped = on_line_stopped() if on_line_stopped else False
        if stopped or not watch_app or app_pid(adb, serial) == pid:
            return
        # 监听期间应用被重启/重装，进程号已变，接上新进程继续输出


def _wait_app_pid(adb: str, serial: str) -> str:
    while True:
        pid = app_pid(adb, serial)
        if pid:
            return pid
        # 应用重启的空档，短暂等待后再试，避免整条流因一次取不到 pid 就退出
        time.sleep(1)


def _stream_once(
    adb: str,
    serial: str,
    tags: list[str] | None,
    pid: str | None,
    on_line: Callable[[str], None],
    on_start: Callable[[subprocess.Popen], None] | None,
) -> None:
    cmd = logcat_cmd(adb, serial, pid)
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        creationflags=NO_WINDOW,
    )
    assert proc.stdout is not None
    if on_start:
        on_start(proc)
    try:
        for raw in iter(proc.stdout.readline, b""):
            # 日志正文可能含非 UTF-8 字节，replace 以保后续行不被中断
            line = raw.decode("utf-8", errors="replace")
            if tags and not any(tag in line for tag in tags):
                continue
            on_line(line)
    except StopStreaming:
        pass
    except ValueError:
        # 读端已被 terminate_tree 关闭，属正常停止路径
        pass
    finally:
        terminate_tree(proc)


def terminate_tree(proc: subprocess.Popen) -> None:
    """结束 logcat 进程，并让阻塞在 readline 上的读取方退出。

    只杀进程不够：adb 被结束后管道仍可能挂着数据，读取方会一直等，因此同时关闭读端。
    注意不要用 `taskkill /T`：未新建进程组时它会把调用方（GUI/命令行）一起带走。
    """
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            subprocess.run(["taskkill", "/F", "/PID", str(proc.pid)], capture_output=True)
    if proc.stdout:
        try:
            proc.stdout.close()
        except OSError:
            pass


def _echo_line(line: str) -> None:
    sys.stdout.write(line)
    sys.stdout.flush()


def cmd_logcat(args: argparse.Namespace) -> int:
    adb = find_adb()
    serial = pick_device(adb, args.device)
    pid = None if args.all_processes else app_pid(adb, serial)
    tags = args.tags or [LOG_TAG]
    if pid is None and not args.all_processes:
        log("应用进程未运行，改为全部进程")
    log("$ " + " ".join(logcat_cmd(adb, serial, pid)))
    stream_logcat(adb, serial, tags, pid, on_line=_echo_line, tail=200)
    return 0


def log_output_path() -> Path:
    # 放在工具目录下，避免污染被托管的 Android 工程（其 build/ 会被 clean 清掉）
    return TOOL_DIR / "build" / "logs" / f"logcat-{time.strftime('%Y%m%d-%H%M%S')}.log"


def cmd_grab(args: argparse.Namespace) -> int:
    adb = find_adb()
    serial = pick_device(adb, args.device)
    target = Path(args.out) if args.out else log_output_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    log(f"开始抓取全量日志（Ctrl+C 结束）：{target}")
    with open(target, "w", encoding="utf-8", newline="\n") as f:
        try:
            stream_logcat(adb, serial, None, None, on_line=lambda line: (f.write(line), f.flush()), since_now=True)
        except KeyboardInterrupt:
            pass
    log(f"已保存：{target}")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    gradle_assemble(args)
    run([GRADLE_WRAPPER, "koverVerify"])
    run([GRADLE_WRAPPER, "detekt"])
    return 0


def cmd_uninstall(args: argparse.Namespace) -> int:
    adb = find_adb()
    serial = pick_device(adb, args.device)
    run([adb, "-s", serial, "uninstall", APPLICATION_ID])
    return 0


COMMANDS = {
    "apk": cmd_apk,
    "build": cmd_build,
    "check": cmd_check,
    "logcat": cmd_logcat,
    "grab": cmd_grab,
    "uninstall": cmd_uninstall,
}


def add_build_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--build-type",
        choices=["debug", "release"],
        default="debug",
        help="构建类型（默认 debug）",
    )
    parser.add_argument(
        "--flavor",
        help="product flavor（多 flavor 工程必填；也可在工程 devtask.config.json 里配置 flavor）"
    )


def add_device_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--device", help="目标设备序列号（多设备时必填，可用 adb devices 查看）")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="devtask", description="Android 工程本地研发任务（编译 / 安装 / 启动 / 日志）")
    parser.add_argument(
        "--project",
        help="目标 Android 工程根目录（默认取 DEVTASK_PROJECT 或 devtask.config.json 的 project）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_apk = sub.add_parser("apk", help="编译指定变体并安装到车机（可选自动启动）")
    add_build_args(p_apk)
    add_device_args(p_apk)
    p_apk.add_argument("--no-install", action="store_true", help="只编译，不安装（无设备时验证构建）")
    p_apk.add_argument("--no-launch", action="store_true", help="安装后不自动启动")
    p_apk.add_argument("--fresh", action="store_true", help="安装前先卸载，清除旧数据")
    p_apk.set_defaults(func=cmd_apk)

    p_build = sub.add_parser("build", help="只编译，不安装")
    add_build_args(p_build)
    p_build.set_defaults(func=cmd_build)

    p_check = sub.add_parser("check", help="提交前门禁：编译 + 覆盖率验证 + Detekt")
    add_build_args(p_check)
    p_check.set_defaults(func=cmd_check)

    p_log = sub.add_parser("logcat", help="过滤本应用日志（默认按配置的 TAG 前缀）")
    add_device_args(p_log)
    p_log.add_argument("tags", nargs="*", default=[], help="匹配关键词，同时匹配 TAG 与正文")
    p_log.add_argument("--all-processes", action="store_true", help="不限定本应用进程")
    p_log.set_defaults(func=cmd_logcat)

    p_grab = sub.add_parser("grab", help="从此刻起抓取全部日志到文件（Ctrl+C 结束）")
    add_device_args(p_grab)
    p_grab.add_argument("--out", help="输出文件路径（默认本工具目录 build/logs/logcat-<时间>.log）")
    p_grab.set_defaults(func=cmd_grab)

    p_uninstall = sub.add_parser("uninstall", help="从设备卸载应用")
    add_device_args(p_uninstall)
    p_uninstall.set_defaults(func=cmd_uninstall)

    return parser


def main(argv: list[str] | None = None) -> int:
    # Windows 控制台默认 GBK，脚本内大量中文字面量会乱码或抛 UnicodeEncodeError
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    if FROZEN and argv is None:
        # 打包后的 exe 双击即进 GUI；GUI 再以 `devtask.exe --cli <子命令>` 复用本入口
        argv = sys.argv[1:]
        if not argv or argv[0] != "--cli":
            import devtask_gui

            devtask_gui.run_embedded()
            return 0
        argv = argv[1:]
    args = build_parser().parse_args(argv)
    try:
        set_project(args.project)
        return args.func(args)
    except TaskError as e:
        print(f"[devtask] 失败：{e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n[devtask] 已中断", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
