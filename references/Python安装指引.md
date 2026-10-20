# Python 环境检查与安装指引

## 先检查已有环境

按以下顺序检查，找到可用解释器就停止查找并继续任务：

1. **当前宿主提供的运行时**：优先使用环境说明或宿主工具返回的 Python 绝对路径。在 Codex 桌面端，若可调用 `load_workspace_dependencies`，使用其返回的 Python executable 路径；工具不可用时继续下一项，不把它作为必需依赖。
2. **系统命令**：检查 `python`、Windows 的 `py -3`、macOS/Linux 的 `python3`，以及用户明确提供的解释器路径。
3. **已知宿主位置**：在 Codex 的 Windows 本机场景，上述方式均不可用时，可检查当前用户主目录下 `.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe`。这是已观察到的候选路径，并非所有版本或系统都保证存在；不写死用户名、不扫描全盘，不猜测其他工具的私有路径。

每个候选都实际运行下方验证：版本不低于3.10，且 `sqlite3`、`ssl` 可导入。文件存在、命令指向应用商店入口或仅能输出版本，均不足以认定可用。宿主自带运行时不一定加入 PATH（命令搜索路径）；系统 `python` 不可用，不代表用户需要安装 Python。仅在上述候选均不可用后，才展示安装指引。

```text
python -c "import sys, sqlite3, ssl; print(sys.executable); print(sys.version); assert sys.version_info >= (3, 10), '需要 Python 3.10+'"
```

下文及 SKILL.md 中的 `python` 均应替换为通过验证的解释器绝对路径；Windows 的带引号路径在 PowerShell 中用 `&` 调用。保留验证返回的 `sys.executable` 供本次流程使用，不修改系统 PATH，不安装额外依赖。只做环境检查时不用询问用户是否安装过 Python。

## 环境不可用时向用户展示

技能包不包含 Python 安装包。请用户自行下载并安装，保留当前任务进度，安装验证成功后继续。

- [Python 官方下载页（英文）](https://www.python.org/downloads/)：优先从这里选择适合操作系统和处理器架构的、仍受支持的 Python 3 稳定版。不固定“最新版本”号，不选预发布版或嵌入式精简包。
- [Python 中文参考下载页](https://python.github.net.cn/downloads/)：可辅助阅读，但未核实官方归属，不能称为官方中文版，内容可能滞后；版本和最终安装包以 python.org 为准。可使用浏览器翻译官方页面。

## Windows 自定义安装位置

以下步骤适用于官网版本发布页提供的传统 `.exe` 完整安装程序。安装管理器界面不同，不套用这些按钮；用户需要图形界面选择安装目录时，优先指导使用完整安装程序。

1. 在安装首屏勾选 **Add python.exe to PATH**（将 Python 加入命令搜索路径）。
2. 选择 **Customize installation**（自定义安装），保留默认标准库相关组件，点击 Next（下一步）。
3. 在 **Advanced Options**（高级选项）中的 **Customize install location**（自定义安装位置）选择目录。不想把 Python 主程序放C盘，可改为 `D:\Tools\Python314` 等本人有写入权限的目录；示例目录按实际版本调整。
4. 核对位置后点击 Install（安装）。自定义的是 Python 主程序目录，不能保证安装缓存等所有文件都不占C盘。
5. 安装后重新打开终端；宿主仍读不到新命令时重启宿主应用，或使用用户提供的解释器绝对路径验证。未加入 PATH 时无需重装。

例如在 PowerShell 中验证自定义目录（以实际路径为准）：

```powershell
& 'D:\Tools\Python314\python.exe' -c "import sys, sqlite3, ssl; print(sys.executable); print(sys.version); assert sys.version_info >= (3, 10), '需要 Python 3.10+'"
```

验证成功后继续原任务；失败时说明缺失命令、版本过低或模块不可用等实际原因。交互优先使用宿主原生选择，不能使用时给出“1. 已安装，重新检测；2. 提供已有 Python 路径；3. 暂停本次任务”。不把等待超时当作安装完成。

官方操作说明：[Python 在 Windows 上的使用](https://docs.python.org/3.14/using/windows.html)。macOS、Linux 按官网对应平台说明指导，不使用 Windows 的盘符和安装按钮。

## 无需第三方依赖

技能只使用 Python 标准库（`urllib`、`hmac`、`hashlib`、`secrets` 等），不需要也不应该执行 `pip install`，不要在用户机器上安装依赖。

- 账密记忆由 `scripts/local_cipher.py` 用标准库实现：密钥本机随机生成，**优先交给操作系统凭据保护**（Windows DPAPI / macOS 登录钥匙串，同样只用标准库），仅在其不可用时才降级为状态目录内的密钥文件。
