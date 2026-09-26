# 1.8.0 可视化面板（观察台）设计文档

- 日期：2026-09-26
- 状态：已与所有者对齐并批准（头脑风暴 2026-09-26 会话）
- 视觉基准样张：[`assets/dashboard-mockup.html`](assets/dashboard-mockup.html)（v5，自截屏验收版本；浏览器直接打开即为像素级目标效果）
- 关联版本：1.8.0（重大更新）

## 0. 决策记录（头脑风暴结论）

| 决策点 | 结论 | 备注 |
|---|---|---|
| 定位 | 纯只读观察台 | 对 agent 循环零侵入、零写入 |
| 技术形态 | stdlib 零依赖 | `ThreadingHTTPServer` + 单文件原生 HTML/CSS/JS，无构建链、无 npm |
| 一期范围 | 四大板块全做 | 项目结构（生长树）/ 运行状态 / 迭代方向 / 成果回顾 |
| 布局 | 双栏工作台 | 左主区 1.72fr（树 + 趋势），右栏 1fr（叙事 + 目标） |
| 核心可视化 | 纵向能力进化树 × 进化卡片流，双向联动 | 域 → 模块 → 文件三层；根上枝下 |
| 树形态 | 纵向生长图 + 双模式 | 默认热力当前态，一键切回放（逐轮生长动画） |
| 视觉 | 双主题（跟随系统 + 手动切换） | 单强调色极简设计系统，参考 Linear/Vercel 气质 |
| 动效 | 流畅且克制 | 四条硬约束，见 §4.6 |
| 进化语义 | 域级归因 + 每轮一句话 | 数据来自 state.history 的 title/summary/自评分，不新造数据 |
| 术语 | 界面用「自评」 | `review_score` = agent 收轮自评（1-5），低于 `review_threshold` 的轮不计达标 |

## 1. 目标与非目标

**目标**

- G1 任何时刻打开 `http://127.0.0.1:<port>/` 即见：运行状态、项目进化史、下一步方向。
- G2 「项目生长」可感知：能力进化树（空间）+ 进化卡片流（时间）+ 回放（过程），回答「项目进化了什么地方、对使用者有什么意义」。
- G3 零侵入：观察台对 `.autopilot` 与仓库只读；ensure 失败绝不阻塞迭代循环（**硬性原则**）。
- G4 零依赖：只用 Python stdlib 与浏览器原生能力；仓库不出现 node_modules、构建产物、外部 CDN。

**非目标（一期不做）**

- 面板写操作（暂停/确认候选/改配置）——控制永远走 CLI；若未来需要，另立设计。
- 远程访问与鉴权——只绑定 127.0.0.1。
- agent 消费的面板——agent 侧效率已由 1.7.0 `round-prep` 覆盖，两者互不依赖。

## 2. 总体架构

### 2.1 进程模型

```
begin-round ──ensure_dashboard()──┬─ 已存活 → 复用（读 dashboard.json）
                                  ├─ pid 已死 → 清理陈旧文件，重新拉起
                                  └─ 未启动 → spawn 分离进程
autopilot dashboard --serve   ← 独立常驻进程（ThreadingHTTPServer）
        │
        ├─ GET /              → dashboard.html（单文件，内嵌 CSS/JS）
        └─ GET /api/snapshot  → JSON 快照（内存缓存）
```

- `autopilot dashboard` 子命令（无 `--serve` 时等价 `--serve` + auto_open 逻辑）：
  - `--port N`（默认 0 = 随机端口）、`--no-open`（不自动开浏览器）、`--stop`（读 `dashboard.json` 终止进程）。
- 状态文件 `.autopilot/dashboard.json`：`{"pid", "port", "started_at", "opened"}`。
  - 探活 = pid 存在；陈旧（pid 死）→ 删除文件并按需重建。
  - `opened` 标记保证浏览器只自动打开一次。
- **run 结束不杀面板**（结束后正好回顾成果）；空闲 30 分钟自退（server 内 `last_request_at`，60s tick 检查），退出时删除 `dashboard.json`。
- ensure 动作由 `begin-round` 触发（唯一挂点），失败只写 warning 到 `log.jsonl`，循环照常。这与 1.7.0 round-prep 的哲学一致：人的工具不进 agent 的关键路径。

### 2.2 端点与安全

- 仅两个 GET 端点：`/`（页面）与 `/api/snapshot`（数据）；其余一律 404。
- 绑定 `127.0.0.1`；不 serve 任意静态文件（无路径拼接，杜绝目录穿越）；无 POST/PUT。
- 页面不展示文件内容，只展示文件名与聚合统计。

### 2.3 快照缓存

- 内存缓存 + 失效条件：`state.json` / `backlog.json` / `analysis.json` / `config.json` 任一 mtime 变化，或 git HEAD 变化 → 重算；否则 2 秒 TTL 直接复用。
- growth 的逐轮 diff 结果按轮缓存在内存（`round → file_changes`）；运行中的 run 只增量 diff 新提交。

## 3. 数据管道（纯函数，全部可单测）

### 3.1 `build_snapshot(repo_root) -> dict`

```jsonc
{
  "meta": {
    "generated_at": "ISO8601",
    "skill_version": "1.8.0",
    "run_id": "...",
    "degraded": ["growth"]          // 数据源缺失/损坏的板块名列表，空数组 = 健康
  },
  "status": {                        // 运行状态板块
    "phase": "running|finished|none",
    "round": 22, "round_seq": 24,
    "completed_rounds": 20, "blocked_rounds": 0, "cancelled_rounds": 2,
    "budget": {"max_minutes": null, "remaining_minutes": null,
               "deadline_remaining_minutes": null, "estimated_tokens_used": 4200},
    "goals": {"total": 5, "met": 3},
    "backlog": {"total": 93, "pending": 16, "ready": 4},
    "expansion_waves": 2
  },
  "growth": {                        // 能力进化板块
    "domains": [{
      "id": "supply", "name": "供给与探矿",
      "meaning": "挖掘器决定迭代上限：backlog 从哪来、为什么值得做",
      "first_round": 8, "active_rounds": [8, 14, 20, 21, 22],
      "weight": 0.9,                 // 生长强度 0-1，编码枝干粗细
      "modules": [{
        "name": "miner.py", "path": "scripts/autopilot/miner.py",
        "first_round": 8,
        "churn": {"touches": 6, "insertions": 180, "deletions": 44},
        "files": ["scripts/autopilot/miner.py"]   // 一期模块=文件或目录级聚合
      }]
    }],
    "events": [{                     // 进化卡片流（全部轮次，含 cancelled/aborted）
      "round": 22, "status": "completed",
      "title": "exhausted 只计 apply run",
      "summary": "…", "score": 4,    // 自评分，可为 null
      "domains": ["supply"],         // 该轮改动归因到的域
      "commit_sha": "20c6a6e"
    }],
    "rounds": [{                     // 趋势与回放轴数据
      "round": 22, "status": "completed", "score": 4,
      "files_changed": 3, "insertions": 41, "deletions": 7
    }]
  },
  "narrative": {                     // 成果回顾
    "has_last_summary": true,
    "retrospective_exists": true
  }
}
```

取舍说明：

- stats 大数字一期展示：轮次、有效轮数、域/模块规模、目标、tokens（**不展示测试总数**——state 里没有逐轮测试计数，编造即撒谎；样张中的 495 在实现中替换为有数据来源的指标）。
- `events` 全量返回（单 run 规模 ≈ 数十轮，JSON 体积可控）；文件叶随模块返回，默认由前端折叠。

### 3.2 模块聚合器 `aggregate_modules(file_changes, domain_map) -> domains`

- 输入 `file_changes`：`[{path, first_round, touches, insertions, deletions}]`（来自 §3.4）。
- 归域规则（按优先级）：
  1. `config.dashboard.domain_map` 自定义映射：键 = 路径前缀，值 = **结构化对象** `{"name": "域名", "meaning": "一句话意义"}`（`meaning` 可省略）；最长前缀优先；命中即归。
  2. 内置通用启发式：`test*`/`tests/` → 测试；`docs/`、`references/`、`README*`、`CHANGELOG*`、`SKILL*` → 文档与知识；`scripts/`、`tools/`、`bin/` → 工具脚本；其余源码目录 → 核心实现；根散文件 → 配置与入口。
  3. 仍未命中 → 「其他」域。
- 每域聚合 `first_round`（min）、`active_rounds`、`weight`（归一化的 touches+Δlines）。
- **默认域词典是通用的**；样张中的「供给与探矿/核心循环」等语义域名来自本仓库的 `domain_map` 配置（dogfooding：随 1.8 给本仓库 `.autopilot/config.json` 预置该映射，属配置不属于代码）。
- 内置域的 `meaning` 使用内置默认释义；自定义域未给 `meaning` 则留空。

### 3.3 事件关联器 `correlate_events(history, round_domains) -> events`

- 输入 `state.history`（每轮 `round/title/summary/review_score/status/commit_sha`）与每轮改动文件归域结果。
- 输出按轮排序的卡片数据；`cancelled`/`aborted` 轮如实入史（前端灰卡）；`review_score` 缺失 → `null`。
- 纯映射，无 IO。

### 3.4 逐轮文件改动 `compute_round_file_changes(repo_root, state)`

- 锚点：`state.history[i]["commit_sha"]`；第 i 轮 diff 区间 = `history[i-1].commit_sha .. history[i].commit_sha`（首轮用 `state.run_start_sha` 起点）。
- 取 `git diff --name-status --numstat <a>..<b>`，解析增删行数与文件路径；重命名（R）计为 churn 归到新路径；空 diff（零工作取消轮）不产生条目。
- `commit_sha` 缺失（旧版本 state）→ `degraded: ["growth"]`，前端树降级为无染色的当前文件树（仍可用）。
- 实现落在 `io.py` 既有 git 子进程封装风格上，不新引依赖。

## 4. 页面设计（以 v5 样张为像素基准）

### 4.1 设计 token（双主题）

CSS custom properties 两套映射（样张 `:root` 为暗色基准）：

| token | 暗色 | 亮色 |
|---|---|---|
| `--bg` | `#0A0A0B` | `#FAFAF8` |
| `--surface` | `#131316` | `#FFFFFF` |
| `--surface2` | `#1A1A1F` | `#F0F0EE` |
| `--line` | `#232329` | `#E4E4E0` |
| `--txt / --txt2 / --txt3` | `#EDEDEF / #9A9AA2 / #5C5C66` | `#1A1A1A / #5A5A62 / #98989E` |
| `--accent` | `#7EE0B8` | `#0E8A64` |
| `--accent-dim` | `#3A6B57` | `#BFE3D6` |
| `--warm` | `#E8A87C` | `#C77A3A` |

- 自动：`@media (prefers-color-scheme)`；手动：页面右上模式胶囊扩展为切换控件，写 `localStorage`，优先级高于系统。
- 字体：界面 system-ui 栈；数据/树/轴 `"Cascadia Code", Consolas, monospace`。

### 4.2 布局

- 顶栏（品牌 + meta + 状态点）→ 统计行（5 个大数字）→ 双栏 grid `1.72fr / 1fr`（左：树；右：进化叙事 + 目标行）→ 回放轴。
- `max-width: 1280px` 居中；<900px 降级单列堆叠（树在上）。

### 4.3 树渲染（核心组件）

- 输入 `growth.domains` + 前端展开状态；**布局器**输出 `{nodes, edges}`：
  - 坐标规则：根固定左上；主干垂直线 `x=root.x` 贯穿全部域；每域从主干水平出枝到域节点圆点；模块层从域节点下挂二级竖线再水平出枝（正交连线，禁止贝塞尔绕圈——v3 教训）。
  - 文字基线对齐：SVG `text y = cy + 4`（12px 字号统一规则，杜绝 v3 错位）。
  - 域枝粗细 = `weight` 映射 1.2–3.2px；颜色 = 活跃时期（最近活跃 `--accent`，其余灰阶）。
- 交互：点域节点折叠/展开模块层；域释义行直接可见（不藏 hover）；「其余已折叠」用行尾 `⋯`。
- 展开状态保存在页面内存（不持久化）。

### 4.4 进化卡片流

- `events` 倒序渲染；最近一条 `completed` 为热卡（accent 左缘 + 圆点高亮）；`cancelled/aborted` 灰卡；卡片 meta 行：`r{N} · 自评 {score}/5 · {status} → {域名}`。
- 点击卡片 → 树中该轮 `domains` 枝干保持全亮、其余淡出（opacity .35，200ms 过渡）；再次点击或点空白取消。

### 4.5 回放

- 轴 `r1 — r{latest}`；播放 = 从 r1 步进到当前轮：每轮把 `first_round == r` 的节点/边插入渲染（stagger 60ms：opacity 0→1 + translateY 8px→0，400ms），对应卡片同步浮现；播放完自动回到当前态。
- 拖动游标 = 跳到任意轮（直接按 `first_round <= r` 过滤渲染，无动画）。
- 一期不自动播放；进页面始终是当前态。

### 4.6 动效四硬约束

1. 只动 `opacity` / `transform`（GPU 合成）。
2. 时长：微交互 150ms、内容过渡 250ms、树节点生长 400ms；统一缓动 `cubic-bezier(.22, 1, .36, 1)`。
3. 回放 stagger 每节点 60ms，单轮总时长封顶 1.2s。
4. `prefers-reduced-motion: reduce` 时全部动效降级为直接呈现（无 animation/transition）——动效永不作为信息唯一载体。

### 4.7 数据刷新

- 前端每 3s `fetch /api/snapshot`；响应 `meta.generated_at` 与上次相同则跳过重渲染；`document.hidden` 时暂停轮询。
- 服务端缓存见 §2.3；前端无本地持久化（localStorage 仅存主题与展开状态）。

## 5. 配置与生命周期

- `config.json` 新增段（默认关闭，老 run 零影响）：

```json
"dashboard": {
  "enabled": false,
  "auto_open": true,
  "port": 0,
  "domain_map": null
}
```

- `init` 新旗标：`--dashboard` / `--no-dashboard`（写 enabled）；`config-set --dashboard / --no-dashboard`、`config-set --dashboard-port N`（沿用 1.6.0 config-set 字段模式，写盘刷新指纹）。
- `validate_config` 扩展：`enabled/auto_open` 为 bool，`port` 为 `0 <= int <= 65535`，`domain_map` 为 `null` 或 `dict[str, {"name": str, "meaning"?: str}]`；非法即拒绝（沿用既有 load 时校验风格）。
- `ensure_dashboard(repo_root, config)`（state.py 或独立小模块）：
  1. `enabled` 为假 → 直接返回，零开销。
  2. 读 `dashboard.json`；pid 活 → 返回（顺带清文件里陈旧 pid 的情形）。
  3. pid 死/无文件 → spawn：`sys.executable -m autopilot dashboard --serve --port {port}`；Windows 用 `CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW`，POSIX 用 `start_new_session=True`；写 `dashboard.json`。
  4. `auto_open` 且本次为新启动 → `webbrowser.open`（失败静默，warning 入 log）。
- `--stop`：读 pid 文件 `terminate()`；POSIX 独立进程组直接 kill；Windows `taskkill` 兜底。
- 端口占用：随机端口模式下由 OS 分配天然规避；显式 `--port` 冲突时换随机端口重试 3 次。

## 6. 错误处理与边界

| 场景 | 行为 |
|---|---|
| `.autopilot` 不存在 | snapshot 返回 `{"error": "no-run"}`；页面显示引导（「在目标仓库运行 autopilot init 后此处将展示运行与进化」） |
| state.json 损坏/缺字段 | 对应板块进 `degraded`，前端灰显 + 原因；其余板块正常 |
| 无 git / 无提交历史 | `growth` 降级为无染色当前树 |
| 旧 state 无 commit_sha | `degraded: ["growth"]`（树无生长染色） |
| 单请求内部异常 | 500 + JSON 错误体，进程不退；线程隔离 |
| backlog/history 极大 | events 全量但字段精简；文件叶按模块聚合；一期不加分页（规模实测 <200KB） |

## 7. 测试策略

1. **纯函数单测**：`aggregate_modules`（合成输入 → 域归集 / domain_map 覆盖 / 最长前缀 / 未知归「其他」/ weight 归一）；`correlate_events`（status 映射、score 缺失 → null、cancelled 入史）；`compute_round_file_changes` 的 numstat 解析（合成 diff 文本，含重命名与空 diff）。
2. **快照契约**：合成 `.autopilot` 夹具 → `build_snapshot` 输出 shape 断言（键集 + degraded 路径）。
3. **生命周期**：`dashboard.json` 读写往返、pid 探活、陈旧清理、`--stop`（以子进程真实拉起后停止）。
4. **HTTP**：随机端口起真 server（线程内），`urllib` 断言 `/` 200 & `text/html`、`/api/snapshot` 200 & 可解析 JSON、未知路径 404；**前后断言 `.autopilot` 目录内容零变化（只读保证）**。
5. **前端契约**：正则抽取 `dashboard.html` 中 `snapshot.xxx` 字段引用，断言 ⊆ `build_snapshot` 输出键集（沿用 1.7.0 `check --brief` 字段集测试模式，防漂移）。
6. **reduced-motion**：字符串断言 CSS 含 `@media (prefers-reduced-motion)`。
7. 版本一致性测试自动覆盖 1.8.0 六处 bump（既有测试，无需新增）。

## 8. 文档同步

- `SKILL.md`：新增「观察台（dashboard）」节——何时开启、只读保证、与 round-prep 互不干扰。
- `references/config.md`：`dashboard` 字段表 + `domain_map` 示例（含本仓库 dogfooding 映射）。
- `references/troubleshooting.md`：端口被占 / server 起不来 / 面板打不开三行。
- `README.md`：一句话提及观察台。
- `CHANGELOG.md`：新增 `## 1.8.0` 段（Added/Docs/Tests）。

## 9. 版本与交付

- 版本权威位六处 bump 至 `1.8.0`：`scripts/autopilot/__init__.py`、`SKILL.md`、`agents/openai.yaml`、`references/overview.md`、`README.md`、`CHANGELOG.md`。
- 交付验收 = 既有 495 测试全绿 + 新增测试全绿 + 真实 `.autopilot` 数据（本仓库 22 轮）上手核验：树与样张观感一致、回放可复现 20 有效轮的生长史。

## 10. 风险与开放问题

- Windows 分离进程的创建旗标组合（`CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW`）需实施时在真机验证；POSIX 路径行为以 `start_new_session=True` 为准。
- `webbrowser.open` 在无默认浏览器的无人值守环境会静默失败——设计上容忍（面板仍可手动打开 URL）。
