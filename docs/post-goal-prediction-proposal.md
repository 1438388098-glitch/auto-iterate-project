# 提案：达 Goal 后新迭代方向的自我预测

> 状态：**待审查**（尚未实现）  
> 范围：补齐 1.2.x Deep Expansion 仍缺的「因刚交付 X，故下一步做 Y」能力  
> 调研：4 路并行（数据模型 / SKILL 协议 / CLI 信号 / ranking 集成）

---

## 1. 问题

当前 expand 本质是「再扫一遍仓库找问题」，不是：

> 刚交付了 X → 因此用户/系统下一步最可能需要 Y

| 缺口 | 现状 |
|------|------|
| 无 goal 链式推理 | `goal-met` 只把字符串写入 `completed_goals`（`commands.py` cmd_goal_met） |
| 不用已完成工作当种子 | phase report / git log / completed_goals 未用于生成新方向 |
| check 不带拓展主题 | expand 时只有 `phase: "expand"`，无 seeds / 主题 / 饱和 type |
| 无假设-验证循环 | 预测、value-gate、开工未结构化；靠 agent 临场发挥 |

已优化过的部分（不要重复做）：16 镜头 Deep Expansion 广度扫描、`backlog-rank` 期望价值、`expand_after_goals` 不停机。

---

## 2. 目标与非目标

### 目标

1. goal 达成后**强制**产出 2–3 条因果假设（因 A 故 B，证据 C）
2. expand 第一波 = **种子波**（消化假设），镜头扫描降为第二波
3. `check` 在 expand 态带出种子 / 已完成 goals / 饱和 type，供 agent 与 subagent 使用
4. seed → candidate → 完成/阻塞 可回写，形成假设命中率闭环
5. 防止预测噪声淹没真实 bugfix（配额 + 置信乘子）

### 非目标（第一版明确不做）

- seed 自动加大权重进 ranking（先观察命中率）
- 从旧 history 反推 seed
- 独立 `direction-hypotheses.json`（并入 `state.json`，少一个迁移面）
- 新 CLI 命令 `predict-directions` / `expansion-scout`（主循环只 poll `check`）

---

## 3. 数据模型（并入 state.json）

### 3.1 不变量

- `completed_goals` **保持字符串数组**，`all_goals_met` / report 逻辑不改
- 旧 `state.json` 经 `migrate_state` 自动补齐新字段
- 无新字段的旧 backlog：ranking / selection 行为与现状完全一致

### 3.2 新字段

```jsonc
// default_state / migrate_state
{
  "goal_events": [],   // 结构化完成记录（种子的“因”）
  "goal_seeds": []     // 预测假设（种子的“果”）
}
```

**`goal_events[]` 单条（goal-met 写入）**

```jsonc
{
  "id": "ge-003",
  "goal": "精确 goal 文本（与 completed_goals 同一字符串）",
  "met_at": "ISO",
  "round": 12,
  "commit_shas": ["abc1234"],
  "candidate_ids": ["candidate-007"],
  "unlocked_capabilities": ["导出模块可 import 且有单测"],
  "recent_commit_topics": ["feat: csv export"],
  "saturated_types": ["feature"],
  "seed_ids": ["seed-011", "seed-012"]
}
```

**`goal_seeds[]` 单条**

```jsonc
{
  "id": "seed-011",
  "source_goal": "精确 goal 文本",
  "source_event_id": "ge-003",
  "title": "把 export 接到 CLI 一级命令并写 --help 测试",
  "hypothesis": "若完成 Y，则用户可从命令行触发已有导出能力，因为模块已存在但无入口",
  "from_capability": "导出模块可 import 且有单测",
  "type": "feature",
  "value": 4,
  "effort": 2,
  "risk": 1,
  "status": "open",
  "created_at": "ISO",
  "promoted_at": null,
  "promoted_candidate_id": null,
  "verified_at": null,
  "outcome": null
}
```

**seed 状态机**

```
open ──backlog-add --from-seed──► promoted ──complete-round──► verified
  │                                  │
  │                                  └── block-round ──► refuted（记 notes）
  └── value-gate 拒绝 ──► rejected
```

**常量（io.py）**

| 常量 | 建议值 |
|------|--------|
| `SCHEMA_VERSION` | 5 → 6 |
| `GOAL_EVENTS_LIMIT` | 20 |
| `SEEDS_LIMIT` | 50 |
| `SEED_TEXT_LIMIT` | 500 |

---

## 4. CLI / check 信号

### 4.1 `goal-met` 扩参

```
goal-met --goal <text>
  [--next-step <title>]              # 可重复 = 一条 seed
  [--unlocked-capability <str>]      # 可重复
  [--seed-type feature] [--seed-value 4] [--seed-effort 2]
  [--no-auto-context]                # 关闭 helper 自动采集 commit/type 快照
```

行为：

1. 仍维护 `completed_goals`（兼容）
2. 自动快照：近期 commit 主题、candidate_ids、saturated_types
3. 每条 `--next-step` 建 seed；type 默认**避开**饱和 type
4. `--json` 的 `data` 增加 `goal_event` / `seeds`（additive，无 exact-set 契约冲突）

### 4.2 `backlog-add --from-seed <id>`

- 从 seed 取 title/type/value/effort/risk 作默认，CLI 显式参数可覆盖
- candidate 额外写 `from_seed` / `hypothesis`
- seed → `promoted`，记 `promoted_candidate_id`

### 4.3 `check`（仅 `phase=="expand"` 时附加）

```jsonc
{
  // ...现有字段不变...
  "expansion": {
    "seeds": [{ "id", "title", "type", "from_capability", "source_goal", "status" }],
    "completed_goals": ["..."],
    "recent_types": { "feature": 4, "test": 1 },
    "saturated_types": ["feature"],
    "underused_types": ["bugfix", "refactor", "perf", "docs"],
    "suggested_themes": ["登录相关安全加固", "docs: ..."],
    "min_pending_candidates": 3
  }
}
```

**契约兼容**：iterate 阶段 **不**出现 `expansion` 键 → `ContractTests.test_check_brief_contract` 不破。  
expand 态另加一条契约测试锁 key set。

### 4.4 complete / block 回写

- `complete-round`：round 内 `from_seed` 候选完成 → seed `verified`
- `block-round`：→ seed `refuted` + notes（避免失败假设反复回流刷分）

---

## 5. SKILL.md 协议（Wave 0 / Wave 1+）

### 5.1 新章：Post-Goal Direction Prediction

**触发**（无人值守默认开，不问用户）：

1. `goal-met` 刚标达某 goal
2. `check` 报告 `phase: "expand"` 后的第一次 expansion
3. 运行中途某 goal 达成且 `continue` 仍为 true

**假设生成（2–3 条，硬性因果句）**

```
因刚完成 A，故优先 B，证据是 C（仓库内可核实事实）。
```

| 因果类 | 模板 | 例 |
|--------|------|-----|
| 使能 enablement | 因 A 打开能力 U，故把 U 接到真实入口/文档/CLI | 刚加完 API X → 补 README 与示例 |
| 暴露 exposure | 因 A 改动表面 S，故加固 S 的测试/错误路径 | 刚改鉴权 → 补越权 401 矩阵 |
| 延续 journey | 用户拿到 A 后下一步会做 J，故让 J 不碎 | 刚打通导入 → 处理导入后首次同步失败 |
| 偿债 debt | 因 A 为赶 goal 绕过 D，故拆掉 D | 硬编码路径 → 配置项 |

**假设-验证循环**

1. Predict：若 H 为真，用户下一步最需要什么？  
2. Evidence check：最小代价核实 C；失效则 demoted/rejected  
3. Value-gate：`backlog-add` + `backlog-rank`（与 Expansion 同一红线）  
4. 取最可信 1–2 条 `begin-round`  
5. round 结束回写 validated / rejected；允许同因果链再派生一层（最多两层）

### 5.2 Expansion 分波

| 波 | 名称 | 输入 | 动作 |
|----|------|------|------|
| **Wave 0** | 种子波 Seed Wave | 刚完成 goal + open seeds | 核实 → value-gate → promote → 开工；**禁止**16 镜头扫仓 |
| **Wave 1+** | 镜头波 Lens Wave | Wave 0 后仍 thin | 现有 Deep Expansion 16 镜头 |

硬性规则：

- goal 刚达成后的第一波必须是 Wave 0  
- Wave 0 已让 ready pending 达标 → 可先干活，不必进 Wave 1  
- 假设全部 rejected/validated 且仍 thin → 升级镜头波  
- 镜头 subagent 的 prompt 附带 open seeds 摘要；命中同一因果链则合并，不双计  

### 5.3 Anti-Idle 追加违规

- goal 刚达成就镜头扫仓，而不是先生成因果假设  
- 假设无「因…故…」或无仓库证据  
- 只生成假设却从不 value-gate / 从不 backlog-add  
- 以「需要用户确认方向」为由在 `continue: true` 时空等  

---

## 6. Ranking 集成（防噪声，刀 B）

### 6.1 候选可选字段

| 字段 | 默认 | 语义 |
|------|------|------|
| `origin` | `observed` | `observed` \| `predicted` \| `expansion` |
| `based_on` | null | 触发预测的 goal 文本 |
| `confidence` | observed=1.0；predicted∈[0.5,1.0] | 成立概率；**不改 value** |
| `evidence` | `""` | 审计用，不进公式 |

### 6.2 公式（`_score_expected`）

```
expected_value
  × confidence_factor        # predicted 用 confidence；observed=1.0
  × goal_chain_factor        # based_on ∈ completed_goals → ≤1.08（小于 unlock 0.15）
  × unlock × risk × saturation × mix / effort
```

`score_breakdown` 增加 `origin` / `confidence` / `confidence_factor` / `goal_chain_factor`。

### 6.3 选批闸门

- **`max_predicted_per_round` 默认 1**：一轮最多吃一个预测方向  
- score 平局时 `observed` 优先  
- predicted 单独记 blocked/review 子账；样本不足用 `success_rate × 0.75` 保守先验  
- run 后期（progress > 0.7）对 predicted 额外收紧  

`ranking_mode: classic` 第一版不接预测因子，保持 legacy 纯净。

---

## 7. 落地顺序（建议两刀）

### 刀 A — v1.3.0 核心闭环（优先）

| 步 | 内容 | 主要落点 |
|----|------|----------|
| 1 | schema + migrate + 校验 | `io.py`, `state.py` |
| 2 | `saturated_types` / `open_seeds` / `append_goal_event` / `append_seed` / `resolve_seed` | `state.py` |
| 3 | `cmd_goal_met` 结构化写入 + CLI 扩参 | `cli.py`, `commands.py` |
| 4 | `check` expand 态 `expansion` 上下文 | `commands.py` |
| 5 | `backlog-add --from-seed` + complete/block 回写 | `cli.py`, `commands.py` |
| 6 | SKILL.md Post-Goal 章 + Expansion Wave 0/1+ | `SKILL.md` |
| 7 | 测试：迁移 / goal-met 双写 / promote / 回写 / check expand 契约 | `test_autopilot_state.py` |

### 刀 B — 防噪声（可同车或 1.3.1）

| 步 | 内容 |
|----|------|
| 8 | `origin`/`confidence` CLI 透传 |
| 9 | `_score_expected` 乘子 + breakdown |
| 10 | `_mark_selection` 配额 + 排序次级键 |
| 11 | predicted 子账校准 |
| 12 | report「方向假设」小节 |

---

## 8. 验收清单

**刀 A**

- [ ] 旧 state 迁移后 `read`/`check` 不炸；`all_goals_met` 不变  
- [ ] `goal-met --next-step` 后 state 含 event+seeds；`completed_goals` 仍为字符串  
- [ ] `check --brief`：iterate **无** `expansion` 键；expand **有** seeds/themes  
- [ ] `backlog-add --from-seed` 后 seed=promoted，candidate 带 `from_seed`  
- [ ] complete/block 自动 verified/refuted  
- [ ] SKILL.md：goal 后第一波必须 Wave 0；Anti-Idle 含种子违规  

**刀 B**

- [ ] 无 `origin` 字段的旧 backlog，rank/selection 与改前一致  
- [ ] 同 value 下 `confidence=0.6` 的 predicted score ≈ observed 的 0.6 倍  
- [ ] 默认一轮 selected 至多 1 个 predicted  
- [ ] predicted 连续 blocked 后后续 predicted 沉底，observed 不被连坐  

---

## 9. 风险与回滚

| 风险 | 缓解 |
|------|------|
| 破坏 check exact-set 契约 | 仅 expand 态加键；iterate 契约测试不动 |
| 预测噪声淹没 bugfix | 刀 B 配额默认 1；第一版 ranking 不加分 |
| schema 迁移踩旧 state | `migrate_state` 幂等回填；双写 `completed_goals` |
| token 成本 | Wave 0 证据核实限定最小代价；全仓扫仍只属 Wave 1+ |
| 回滚 | 刀 A/B 各自独立提交；schema 新字段对旧 reader 忽略即可 |

---

## 10. 版本与文档同步（实现时）

- `SKILL.md` / `README.md` / `agents/openai.yaml` / `CHANGELOG.md` → **1.3.0**  
- `references/config.md`：`goal-met` 新参、`backlog-add --from-seed`、check expansion 字段、（刀 B）`max_predicted_per_round`  
- 测试基线：在现有 210 用例上增量，不删既有契约  

---

## 审查问题（请在 PR / 提交评论中回复）

1. **刀 A / 刀 B**：同意分两刀？还是必须同车进 1.3.0？  
2. **seed 存 state.json**：同意，还是要独立 `seeds.json`？  
3. **`max_predicted_per_round` 默认 1**：是否过严/过松？  
4. **goal-chain 加成上限 1.08**：是否接受（刻意小于 unlock 0.15）？  
5. **block → refuted（不回 open）**：是否接受「失败假设不回流刷分」？  
6. **是否需要** `seed-list` / `seed-outcome` 显式命令（当前计划用自动回写，可不加）？
