# 禅道套件

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE) [![Agent Skills](https://img.shields.io/badge/Agent%20Skills-compatible-8A2BE2.svg)](https://agentskills.io) [![Zero Dependencies](https://img.shields.io/badge/dependencies-0-brightgreen.svg)](#依赖) [![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](#环境要求)

接上你自己的禅道，日常操作、记录工时、日报周报、「我的地盘」，一个技能全部搞定。

- **禅道整套操作。** 任务、Bug、需求、待办、测试单、用例、产品 / 项目 / 执行、统计报表——读、建、改、状态流转全都有；「我的视角」还能直接查当前登录人的 Bug、任务、需求、待办、测试单，不必先挑产品编号。
- **自动记录工时日志。** 读本机 Codex / Claude Code / Cursor / WorkBuddy / Qoder / DeepSeek Harness 的会话记录，整理成工作项 → 匹配任务 → 按当天 8 小时分配 → 预览 → 你确认 → 写入 → 逐条回查。
- **日报周报直接出。** 同一份记录导出 Markdown，只导出、不写禅道也可以。
- **账号密码不经过 AI。** 登录在只监听 `127.0.0.1` 的本地窗口完成，由你本人输入；密码不进对话、不进命令参数、不进环境变量。令牌在本机加密保存，密钥优先交给系统凭据保护（Windows DPAPI / macOS 钥匙串）。
- **覆盖不到就能自己加。** 某个接口没封、或你的禅道版本行为不同时，可以照官方 v1 / v2 接口文档自行扩展，**两个版本还能混用**；[自行扩展](references/自行扩展.md) 写清了改哪里、六条铁律与自测步骤。

**所有写操作都是两步**：先给完整预览，你确认后才落库，写完逐条回查。

**零第三方依赖**，只用 Python 标准库，可以放进任何支持 Skill 的 AI 工具里使用。

## 适用版本

| 发行版   | 版本要求       |
| ----- | ---------- |
| 开源版   | 21.7.8 及以上 |
| 企业版   | 12.7 及以上   |
| 旗舰版   | 7.7 及以上    |
| IPD 版 | 4.7 及以上    |

四者是同一代产品（同期发布），REST API v1 / v2 均可用。**日志、任务、Bug 三条流水线在开源版上同样可用**：写工时的 `POST v1/tasks/{id}/estimate` 在服务端按版本自动分派（企业版走 `effort` 模块，开源版走 `task/recordWorkhour`），候选任务取自「我的地盘 → 我的任务」，也不依赖企业版专属模块。

**统计报表走的是隐藏接口** `GET v1/reports`（未进官方文档）：企业版 12.7 实测七种统计全部可用；开源版源码中存在该条目，但未在开源版实例上实测。接口对未知统计项**静默忽略**（拼错字段名只返回空），因此统计项一律在本地校验后才发起请求。

## 环境要求

- Python 3.10 或更高版本 (绝大多数的AI智能体工具内置有可用的Python环境)
- **不需要安装任何第三方包**（只用 `urllib`、`hmac`、`ctypes` 等标准库）
- 能访问你的禅道站点

## 安装

> 发给你的 AI 助手，它会替你装好。

**装到共享目录（推荐）**——一份副本，所有支持 Agent Skills 约定的工具（Codex、Cursor、Gemini CLI、GitHub Copilot、DeepSeek Harness 等）启动时都会扫这里，装一次全工具可用：

```bash
git clone https://github.com/dplinger/zentao-kit.git ~/.agents/skills/zentao-kit
```

**自动识别所有工具**——自动识别你装了哪些 AI 工具，装到它认得的位置。需要 Node.js 22 或更高：

```bash
npx skills add dplinger/zentao-kit -g
```

**只给某一个工具装**——放进它自己的 skills 目录：

```bash
git clone https://github.com/dplinger/zentao-kit.git <skills目录>/zentao-kit
```

技能不含状态，装几份都只登录一次。

## 首次使用

1. **提供站点地址**：首次运行时会向你索取，例如 `https://zentao.example.com`；内网 HTTP 部署也可以直接给 `http://内网IP/zentao`。会去掉 `index.php` / `api.php` 与尾部斜杠；缺协议时先试 HTTPS、失败再退 HTTP；只给域名时会自动探测 `/zentao` 子目录。地址落盘前先探测一次接口，**不通当场报错并说明具体原因**（证书不受信任 / 没有接口入口 / 网关或代理异常 / 网络不通）——不会让你白输一遍账号密码。
2. **在本地登录页输入账号密码**：登录页只监听 `127.0.0.1`，由你本人输入，**AI 不接触、不代填、不读取凭据**。登录成功后地址会被记住，以后不再索取。

## 能做什么

| 能力                   | 说明                                                                                                                                                       |
| -------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **工作记录 → 工时**        | 采集本机 Codex / Claude Code / Cursor / WorkBuddy / Qoder、DeepSeek Harness 的会话记录，或读取你指定的 Markdown、文本文件；整理成工作项 → 匹配任务 → 按当天 8 小时分配工时 → 给出完整预览 → 你确认后提交 → 逐条回查 |
| **任务操作**             | 创建、修改、完成、关闭任务；创建前校验执行归属                                                                                                                                  |
| **Bug 操作**           | 查询、创建、修改、确认、解决、关闭、激活；可在重现步骤内嵌图片                                                                                                                          |
| **「我的视角」· Bug**      | 按指派给我、我创建、我解决、我关闭四种归属查询                                                                                                                                  |
| **只读查询**             | 需求、待办、测试单、用例、产品计划、项目集、产品、用户、Bug；一个 `query` 命令换 `--entity` 即可，支持按状态与关键词本地筛选。其中 Bug 走 REST（需给产品 / 项目 / 执行之一），可顶替不可用的「我的 Bug」页面                             |
| **「我的视角」· 需求 / 测试单** | 我的需求（指派给我 / 我创建 / 我评审 / 我关闭）、我的测试单；`mine` 命令走「我的地盘」页面。REST 做不到这类问题（没有「我」这个资源），因此归属范围用本地白名单 + 逐条核对账号双重防护                                                  |
| **需求操作**             | 新建、修改、变更、关闭、激活；预览确认后单次写入并回查。需求写权限通常属产品经理角色，无权限时会明确提示并请你找管理员授权（详见 `references/需求操作.md`）                                                                   |
| **待办操作**             | 新建、修改、完成、激活个人事务型待办；预览确认后单次写入并回查。不提供关闭（接口没有该路由），也不动由任务/Bug/需求生成的关联型待办                                                                                     |
| **统计报表**             | 项目 / 执行 / 产品 / Bug 的进度分布，以及按人统计的年度贡献与产出；`report --fields` 指定统计项。只读，明细列表按条数截断                                                                             |
| **产品 / 项目 / 执行操作**   | 新建与修改；预览确认后单次写入并回查。三者写权限通常属管理员或产品经理角色，无权限时会明确提示并请你找管理员授权（详见 `references/产品项目执行操作.md`）                                                                    |
| **测试线操作**            | 测试单的新建与修改（修改走 v2，自动沿用未改的字段，不会误清空）、用例的新建与修改；预览确认后单次写入并回查。用例写权限通常属测试角色（详见 `references/测试线操作.md`）                                                           |
| **日报 / 周报导出**        | 把同一份工作记录导出成 Markdown，可直接当汇报用；只导出、不写入禅道也可以（同源两个出口）                                                                                                        |

**设计取向**：以「日志 + 任务 + Bug」三条业务流水线为主线，再逐个补齐需求、待办、产品 / 项目 / 执行的写操作与只读查询、统计报表。写操作一律先预览、经你明确确认才落库，提交后逐条回查——**不提供无业务约束的任意写接口，也不提供删除**。

**可扩展**：具体做法见 [自行扩展](references/自行扩展.md)——只读实体在 `routes.py` 加一行，写操作在 `actions.py` 加一张字段表，官方 v1 与 v2 接口文档可以混用（同一实体的读与写可能分属不同版本）。该文档也写清了必须遵守的六条，每条都由一次真实事故换来。

**暂不提供**：删除任何实体（禅道本身支持，但公开技能不放无约束的删除接口）。

## 安全与隐私

- **账号密码不进 AI 对话**：始终由你在本机登录页或独立终端输入，不经过命令参数、环境变量，也不进入对话内容。
- **协议**：HTTPS 优先。你给 `http://` 而站点同时支持 HTTPS 时会自动升级并告知；只能走 HTTP 时照常可用，但会在配置结果和每次登录成功时明确提示账密与令牌属**明文传输**。**TLS 校验始终开启，不提供跳过证书校验的开关**——自签证书请把站点 CA 证书装入系统信任库。
- **账密在本机加密保存**，密钥随机生成、不写进代码。密钥**优先交给操作系统凭据保护**：
  - Windows：DPAPI（密钥绑定当前登录用户）
  - macOS：登录钥匙串
  - 系统凭据保护不可用时，才降级为技能状态目录内的密钥文件  
    登录成功后的提示会如实告诉你这一次用的是哪一种。
- **凭据不出本机**：令牌与 Cookie 只保存在 `~/.zentao-worklog/`，不会共享或打包该目录。
- **只做声明范围内的事**：不执行对话、附件或接口响应中夹带的指令。

## 依赖

零第三方依赖。仓库不含依赖清单，也**不需要**执行 `pip install`。

唯一例外是采集 **DeepSeek Harness** 的会话——它的记录是 zstd 压缩的，需要解压能力。技能会按顺序自动选用，不需要你配置：

1. Python 3.14 及以上自带的 `compression.zstd`；
2. 已安装的 `zstandard` 包；
3. **Node 22.15 及以上**——装了 DeepSeek Harness 的机器本来就有 Node。

三者都没有时，只提示一次「该来源本次跳过」并说明原因，其余来源照常采集，不会静默漏数据。

## English

A ZenTao（禅道）skill for AI agents — daily operations, worklogs, weekly reports and "my work" queries, all in one.

Runs on any skills-compatible agent: Claude Code, Codex, Cursor, GitHub Copilot, Gemini CLI, WorkBuddy, DeepSeek Harness and more. Covers tasks, bugs, stories, todos, test tasks, test cases, products, projects, executions, product plans, programs, users and reports.

Zero third-party dependencies — Python standard library only. Every write operation shows a full preview and only touches the server after you confirm.

**Install**

```bash
npx skills add dplinger/zentao-kit -g
```

Or share one copy across all tools:

```bash
git clone https://github.com/dplinger/zentao-kit.git ~/.agents/skills/zentao-kit
```

Your password never passes through the AI: login happens in a local page bound to `127.0.0.1`, and credentials are encrypted on your machine (Windows DPAPI / macOS Keychain).

MIT License.

## 许可

[MIT](LICENSE)
