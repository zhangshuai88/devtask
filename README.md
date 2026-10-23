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
  "package": "com.example.zspractice",
  "launchActivity": ".MainActivity",
  "logTag": "Customize_",
  "flavor": "productA"
}
```

包名与启动页**默认从工程 manifest 自动识别**：包名取 `package`（没有则 `namespace`），启动页取带
`MAIN` + `LAUNCHER` intent-filter 的 activity；优先读已合并的 manifest（`app/build/intermediates/merged_manifest/`），
没有再读 `app/src/*/AndroidManifest.xml`。所以不同工程不用各自写死首页名，`launchActivity` 只在自动识别不准时才需要配。

## product flavor

构建任务按 `<flavor><BuildType>` 组装，例如 `:app:assembleProductADebug`：

- 工程只有一个 flavor，或不带 flavor：仍执行 `:app:assembleDebug`，行为不变。
- 多个 flavor：必须显式指定——命令行 `--flavor productA`，或在该工程 `devtask.config.json` 里写 `"flavor"`；
  GUI 在有多个 flavor 时显示「Flavor」下拉框。都不给会直接报错并列出可选 flavor。
- 不指定时 Gradle 会把所有 flavor 都构建一遍，但产物在各 flavor 子目录下（`apk/productA/debug/app-productA-debug.apk`），
  `apk/debug/app-debug.apk` 根本不存在——这正是以前"装出来不符合预期"的原因。
- flavor 与产物路径从 AGP 生成的 `app/build/outputs/apk/*/*/output-metadata.json` 反推，所以**工程至少构建过一次**；
  从未构建过且未指定 flavor 时退回 Gradle 默认变体。

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
python devtask.py build --flavor productA --build-type release   # 多 flavor 工程
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

## 打包成 exe

`build-exe.cmd` 用 PyInstaller 生成 `dist\devtask\` 文件夹（约 28 MB，压缩后约 12 MB），双击 `devtask.exe` 即开 GUI，对方无需安装 Python。

```bash
pip install pyinstaller
build-exe.cmd
```

- **分发包是整个 `dist\devtask\` 文件夹**（`devtask.exe` + `_internal\`），打包成 zip 发出去；缺了 `_internal` 无法运行。
- 用的是 onedir 而不是 onefile：onefile 每次启动都要把 26 MB 内容解压到临时目录，冷启动约 30 秒，还会在 `%TEMP%` 留下 `_MEI*` 目录；onedir 启动实测 0.3 秒。
- `devtask.config.json` 放在 exe 旁边（`build-exe.cmd` 会自动复制一份，`project` 指向对方的工程）；没有的话启动会弹窗提示并退出。
- 同样的 exe 也能当命令行用：`devtask.exe --cli build --build-type debug`。GUI 内部调用子命令时也走这条路径，所以打包后只有一份代码。
- 配置与日志写在 exe 所在目录（打包后 `TOOL_DIR` = exe 目录），换机器拷贝整个文件夹即可。
- 工具本身打进去的只有 Python 代码；`adb`、JDK、Gradle 仍依赖对方机器环境。

## 注意

- 子进程统一带 `CREATE_NO_WINDOW`：pythonw 启动的 GUI 没有控制台，否则每次调用 adb/gradle 都会闪一个黑窗。
- 「中止当前任务」用 `taskkill /F /T`，任务进程已用 `CREATE_NEW_PROCESS_GROUP` 单独建组，不会连带结束看板自身。
- 门禁命令（`check`）依赖目标工程本身的 task；不含 kover/detekt 的工程会失败。
