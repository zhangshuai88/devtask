# devtask — Android 工程本地研发任务入口

把"编译 → 安装 → 启动 / 看日志"这类重复操作收敛成一个按钮或一条命令。工具本身独立于任何工程，默认指向
`devtask.config.json` 里配置的工程，也可以随时切换。

- **图形界面**：双击 `devtask-gui.cmd`（或 `python devtask_gui.py [工程目录]`）
- **命令行**：`python devtask.py [--project <工程目录>] <子命令>`

只依赖 Python 标准库（tkinter 为官方发行版自带），无需 pip 安装。

## 工程定位

按优先级取第一个含 `gradlew`/`gradlew.bat` 的目录：

1. 命令行 `--project <路径>`，或 GUI 的参数 `python devtask_gui.py <路径>`
2. 环境变量 `DEVTASK_PROJECT`
3. 本目录下 `devtask.config.json` 的 `project`
4. 当前工作目录

GUI 顶部的「选择工程…」会切换工程并写回 `devtask.config.json`，下次启动沿用。

可选地，在**目标工程根目录**放 `devtask.config.json` 覆盖默认值（缺省项不写即可）：

```json
{
  "package": "com.mercedes.customize",
  "launchActivity": ".home.HomeActivity",
  "logTag": "Customize_"
}
```

## 图形界面

左侧一栏是按钮与选项，右侧是统一输出窗口：

| 按钮 | 等价命令 |
|---|---|
| 编译 + 安装 + 启动 | `devtask.py apk` |
| 只编译 | `devtask.py build` |
| 提交前门禁 | `devtask.py check` |
| 卸载应用 | `devtask.py uninstall` |
| 查看应用日志 | 内嵌实时日志，按钮变为「停止日志」 |
| 抓取全部日志 | 从点击时刻起抓全系统日志写文件，按钮变为「停止抓取」 |
| 打开 APK 目录 / 打开日志目录 | 打开资源管理器 |

**应用日志**：只抓本应用进程（`adb logcat --pid`），再按配置的 TAG 关键词过滤；应用重启或重装导致进程号变化时会自动重连。应用未启动时会提示先启动。
设备侧的 `logcat -s Customize_*` 不生效（`*` 被当字面量），本机车机的 `--regex` 实测同样无效，所以关键词过滤放在主机侧做。

**本机车机 logcat 的坑**：`--pid` 与 `-t`/`-T` 一起用时，logcat 会丢掉跟随模式，打印完缓冲就退出（进程 pid 缓冲很小，往往只有十几行，过滤后为 0 行，界面像是"秒停"）。所以历史回放改成独立的 `adb logcat --pid -t N -d` 一次性命令，实时流只用不带 `-t`/`-T` 的 `--pid` 跟随。

**全量日志**：不加任何进程/关键词过滤，先 `-T <点击时刻> -t 200 -d` 回放一小段，再跟随实时日志，默认写到本工具目录的 `build/logs/logcat-<时间>.log`（放在工程外的原因：工程的 `build/` 会被 clean 清掉；每条立即落盘，异常退出也不丢）。输出量很大（每秒数千行），界面只显示行数统计和每秒一条抽样，完整内容看文件。点击停止结束抓取后会自动打开日志所在文件夹。

状态区实时显示当前连接的设备、APK 产物大小与构建时间、车机上已安装的 versionName；设备每 5 秒自动刷新。
运行状态显示在输出区标题上（如「输出 · 编译 + 安装 + 启动中」「输出 · 日志监听中」），完成后变为「… · 完成」或「… · 失败（退出码 N）」。
任务执行中按钮会禁用，同时出现「中止当前任务」按钮，任务结束自动隐藏。

**功能说明**：点右上角「功能说明」弹出一次性的说明窗口，按「顶部 / 环境 / 选项 / 操作」分组列出窗口里每个按钮、选项和状态的含义（表格左侧是界面上的名称，右侧是作用，与界面一一对应）；窗口已打开时再点会提到前面而不是叠出多个，`Esc` 或「关闭」退出。说明内容集中在 `devtask_gui.py` 顶部的 `HELP_SECTIONS`，新增按钮时补一行即可。

启动脚本 `devtask-gui.cmd` 必须保持纯 ASCII：cmd.exe 按 GBK 读取批处理文件，中文注释里的全角标点会被当成命令分隔符报错。

## 命令行

```bash
python devtask.py apk              # 编译 debug 并安装到车机（自动启动配置的 Activity）
python devtask.py apk --fresh      # 清掉旧数据后重装
python devtask.py apk --no-install # 没有车机时只验证编译
python devtask.py build --build-type release
python devtask.py check            # 编译 + koverVerify + detekt
python devtask.py logcat           # 跟本应用日志
python devtask.py logcat MyTag     # 按关键词过滤
python devtask.py grab             # 抓全量日志到本目录 build/logs/（Ctrl+C 结束）
python devtask.py grab --out D:/tmp/x.log
python devtask.py --project D:/MB/i3/settings build
```

## 代码结构

- `devtask.py`：工程定位（`set_project`）、gradle / adb 命令、日志流，以及 `COMMANDS` 子命令注册表
- `devtask_gui.py`：tkinter 看板，只做编排与展示，执行逻辑全部复用 `devtask.py`
- `devtask.config.json`：默认工程路径（GUI 切换工程时写回）

新增命令：写一个 `cmd_xxx(args)`，在 `COMMANDS` 与子解析器里各注册一行即可。

## 注意

- 子进程统一带 `CREATE_NO_WINDOW`：pythonw 启动的 GUI 没有控制台，否则每次调用 adb/gradle 都会闪一个黑窗。
- 「中止当前任务」用 `taskkill /F /T`，任务进程已用 `CREATE_NEW_PROCESS_GROUP` 单独建组，不会连带结束看板自身。
- 门禁命令（`check`）依赖目标工程本身的 task；不含 kover/detekt 的工程会失败。
