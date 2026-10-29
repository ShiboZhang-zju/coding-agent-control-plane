# 为什么跑 agent 时屏幕上不断有终端一闪而过

实测（2026-10-29，CodeBuddy 2.169.0，Windows 11），证据文件：
`T15-console-flash.json`（可见窗口采样）、`T15b-process-flash.json`（进程表 100ms 采样）。

## 1. 结论

**不是控制平面脚本，是 CodeBuddy worker 自己在不停地拉起短命的控制台子进程。**

单次 **agent** 派发，45 秒窗口内新增进程 **242 个**，其中 **217 个存活 ≤3 秒**：

| 进程 | 次数 | 谁拉的 |
|---|---|---|
| `conhost.exe` | 74 | 每个控制台子进程的宿主（真正"窗口"来源） |
| `git.exe` | 61 | agent 工具调用 / hook / worktree 操作 |
| `bash.exe` | 40 | agent 的 Bash 工具（Git Bash） |
| `node.exe` | 31 | CLI 会话、prewarmed worker |
| `powershell.exe` | 9 | agent 的工具调用 |
| `reg.exe` / `cmd.exe` | 7 / 4 | 环境探测、hook |
| `longmemory.exe` | 2 | `SessionStart` / `UserPromptSubmit` hook |
| `sh/sed/awk/grep/head/cut/which/hostname` | 共 11 | Bash 管道 |

对比：**shell 派发**（`bash:true`）只新增 1 个窗口类 `PseudoConsoleWindow`（存活约 3 秒）。

## 2. 两个放大器

1. **agent 的每个工具调用都是一次进程创建**：模型跑 `git status`、`ls`、`cat` 都会起
   `bash.exe` + `conhost.exe`；4 路并行就是 ×4。
2. **hooks**：`~/.codebuddy/settings.json` 里 `SessionStart` / `UserPromptSubmit` / `Stop`
   各挂了一个 `longmemory.exe`，每次派发/结束都会拉起。

`windowsHide` 在 CLI 里有 95 处使用，但 ConPTY（`PseudoConsoleWindow`）与部分
Git Bash 子进程仍会短暂创建可见的控制台窗口，所以看到的是"闪一下就没了"。

## 3. 能做什么

| 手段 | 效果 |
|---|---|
| **把服务放到隐藏桌面启动**（`scripts/serve_hidden.py`） | **实测窗口数 0**（见 §4） |
| 串行派发（不要 4 路同时） | 瞬时窗口数按并发度线性下降 |
| 能用 shell job 就别用 agent job | 窗口从"几十个"降到 1 个 |
| 关掉或改异步 `longmemory` hook | 每次派发少 2–6 次闪窗（顺带解决 T11 并行失败，见 `../isolation/T11-parallel-blocked.md`） |
| 在 Linux/WSL 宿主上跑 worker | 没有 Windows 控制台窗口问题 |
| 反馈给 CodeBuddy | 统一 `CREATE_NO_WINDOW` + ConPTY 隐藏即可根治（本 PoC 无权限改） |

## 4. 已验证的根治方案：把 worker 放到隐藏桌面

控制台窗口总是创建在**启动它的进程所在的那个桌面**上。所以只要服务本身活在一个
非交互桌面上，它派生的所有 `conhost`/`PseudoConsoleWindow` 都不会出现在你的屏幕上。

```powershell
# 隐藏桌面启动（服务器仍在 127.0.0.1:8787，密码认证不变）
$env:POC_HIDDEN_ACTION="start"; python scripts/detached.py start hidden scripts/serve_hidden.py
python scripts/serve_hidden.py status
python scripts/serve_hidden.py stop
```

实测对比（`T15-hidden-desktop.json` vs `T15-default-desktop.json`）：

| 启动方式 | shell 派发可见窗口 | agent 派发可见窗口 | 任务是否正常 |
|---|---|---|---|
| 默认桌面（`serve_ctl.py`） | **1**（`PseudoConsoleWindow` 5.89–8.81s） | 0–1 | 是 |
| 隐藏桌面（`serve_hidden.py`） | **0** | **0** | 是（`t15.txt` 正常生成） |

隐藏桌面下进程照旧创建（40 秒采样：148 个新进程，conhost 44、bash 38、node 19），
只是**窗口不可见**。

注意事项：

- 启动参数用 `CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP` + `STARTUPINFO.lpDesktop`；
  只用 `DETACHED_PROCESS` 时服务在隐藏桌面上起不来（实测 40s 未监听）。
- 负责创建桌面的 launcher 必须存活（`scripts/detached.py` 已在后台跑它）；
  先 `serve_hidden.py stop` 再 `serve_ctl.py stop`，避免残留。
- 隐藏桌面上的会话无法做任何交互式确认，所以 `--auth password` 与只绑 `127.0.0.1` 更重要。
- 测试期间 CLI 自动升级到 **2.178.1**（原 2.169.0），两种桌面下行为一致。

## 4. 更正先前的说法

之前说"改用 detached 运行器后不会再产生可见窗口"——那句只对**我自己的测试进程**成立
（`serve_ctl.py` 用 `DETACHED_PROCESS`，`detached.py` 也不开新窗口）。
worker 拉起的子进程窗口不归我的脚本管，所以 agent 派发时仍会闪。
