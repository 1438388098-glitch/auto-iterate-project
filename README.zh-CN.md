[English](./README.md) · 简体中文

# Auto Iterate Project

版本 1.11.0 — 发布历史见 [CHANGELOG.md](CHANGELOG.md)。

在 agent 会话中自动迭代任意 git 项目：分析仓库、挑选下一个高价值改进、实施小步变更、验证、提交，然后循环往复，直到达成目标，或配置的预算（轮数 / 分钟数 / token 数 / 绝对截止时间）用尽。

支持 opencode、Claude Code、Codex 及其他 agent 运行时（自动检测）。Git 是唯一的外部依赖。

## 为什么做这个

"持续改进这个项目"通常以两种方式之一失败：agent 闲着不动（"看不出还有什么明显可做的"），或者不做验证地空转打转。这个 skill 以确定性的方式接管整个循环——可见的 backlog、逐候选验证、批量提交、硬性的提前停止门控，以及反空转契约——让多小时的自主运行在无需逐步人工批准的情况下保持诚实。它已经在自己的仓库上运行过：1.6.0 版本就是一次 20 轮自托管过夜迭代的产物。1.7.0 则是在此基础上的一次有度量的效率优化——单轮 agent 运行的成本在于它读取的 JSON 与 helper 的调用次数，而不在于 helper 自身的运行时长。

## 快速开始

在任何受支持的运行时中，告诉你的 agent：

```text
Use the $auto-iterate-project skill to autonomously improve this git project.
迭代到明天早上 8 点 / iterate until 8 AM (deadline), or set --max-rounds / --max-minutes / --max-tokens.
```

skill 的入口是 [SKILL.md](SKILL.md) —— agent 读取它、运行 `detect-agent`、在你的仓库里初始化 `.autopilot/` 状态，然后开始轮循环。你通过人工检查点（`checkpoint_every`）、常设规则（`directive-add`），以及可在运行中途更改的预算（`config-set --clear-max-rounds` 等命令）始终保持控制权。

## 一轮如何运作

1. **check** —— 确定性的停止条件、预算与 `action_hint`（work / mine / expand / stop）。
2. **mine** —— 七个扫描器把仓库事实（标记、被吞掉的异常、语法错误、测试缺口、热点、死导出、文档漂移）转化为有证据支撑的 backlog 候选；基于判断的 Deep Expansion 遍历轮换的 16 视角（lens）集合，属于第二波，绝不作为第一波。
3. **rank** —— 按每轮期望价值为候选打分，带多样性配额、价值下限、依赖解锁，以及透明的 `score_breakdown`。
4. **implement → verify → commit** —— 每个候选自成独立单元；每 N 轮做一次完整验证，且提交前必定验证；对每个暂存 diff 做密钥扫描。
5. **record** —— 轮历史、token 记账、每 10 轮一份阶段报告、`finish` 时的一次复盘。

## 亮点

- **状态自有**：`.autopilot/`（config、state、backlog、directives、logs）——可恢复、可迁移、损坏时干净地失败，且每次状态写入都保留一代 `.bak`。
- **诚实的记账**：未验证的目标会阻止"所有目标已达成"的停止；零工作量的取消不消耗预算；只要还有实际工作，`finish` 就会被拒绝，除非某个停止条件（或你本人）另有指示。
- **安全护栏**：绝不重写 git 历史，未经配置绝不 push，拒绝把既有的用户改动吸收进 autopilot 提交；`allow_paths`/`deny_paths` 白名单；密钥模式（AWS / 私钥 / GitHub / Slack / Google / `sk-*` / JWT）。
- **分支生命周期**：feature 模式的运行会回收失效的 `autopilot/*` 分支——`init` 清理已合并的遗留分支，`finish` 在运行分支已合并后将其删除，`branch-gc` 在后续合并之后清理。未合并的工作永不删除。
- **确定性 helper**：以上一切通过 `scripts/autopilot_state.py`（Python 3.6+，仅标准库）运行，由一套精简的约 190 个测试覆盖，横跨 Linux 与 Windows 上的 3.8–3.13（`--smoke` 用于约 1 秒、视机器而定的轮循环检查）。
- **观测面板（可选）**：一个仅限 127.0.0.1 访问的只读 web 面板，在运行开始时打开——进化树（domain → module → file）与逐轮卡片流并排展示，支持回放。树在 init 时由一次全项目扫描播种（state.project_map：完整骨架、每模块文件数），各轮再把活动叠加其上；token 统计是"变更等价"的代理指标，并非 LLM 用量。纯标准库实现；主循环从不依赖它（`dashboard --stop` 可将其关闭）。

## 安装

把该文件夹复制（或链接）到你 agent 的 skills 目录——例如 `~/.claude/skills/auto-iterate-project/`、`~/.config/opencode/skills/auto-iterate-project/` 或 `~/.agents/skills/auto-iterate-project/`。用 git junction/symlink 安装，可通过 `git pull` 让已安装的 skill 持续升级。面向 agent 的总览见 [references/overview.md](references/overview.md)。

## 文档

- [SKILL.md](SKILL.md) —— agent 遵循的操作契约（设置、轮循环、停止条件、升级上报）。
- [references/config.md](references/config.md) —— 所有配置项、默认值与命令行旗标。
- [references/troubleshooting.md](references/troubleshooting.md) —— 症状 → 原因 → 修复。
- [references/expansion-lenses.md](references/expansion-lenses.md) / [references/wave0-prediction.md](references/wave0-prediction.md) —— Deep Expansion 与目标达成后的方向预测。
- [CHANGELOG.md](CHANGELOG.md) —— 发布历史。

## 开发

```bash
# full suite (Windows example; use python3/python consistently elsewhere)
py -3.13 scripts/test_autopilot_state.py
# layered verification (1.9+): between-round fast check (~1s, machine-dependent; fast classes)
py -3.13 scripts/test_autopilot_state.py --smoke
# parallel variants (full / smoke subset)
py -3.13 scripts/test_autopilot_state.py --jobs 4
py -3.13 scripts/test_autopilot_state.py --jobs 4 --smoke
```

CI 在 Python 3.8 与 3.13、Ubuntu 与 Windows 上运行该测试套件。helper 以 Python 3.6+ 为目标，坚持仅依赖标准库的策略。
