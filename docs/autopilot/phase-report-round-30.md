# Auto Iterate 运行报告

## 运行信息

- 仓库: `D:\Claudeworkspace\auto-iterate-project`
- run_id: `044c9e6a558b`
- 活动分支: `autopilot/044c9e6a558b`
- started_at: `2026-10-08T15:42:49.307856+00:00`
- last_activity_at: `2026-10-08T20:28:20.458402+00:00`
- 定时截止: `2026-10-08T23:42:49.253116+00:00`
- finished_at: `无`
- stop_reason: `无`

## 目标

- [x] 完成 config 字段注册表重构（candidate-314）：config-set 与字段校验统一注册表驱动，消除散落分支，全量测试全绿
- [x] 清空 1.13 遗留清单：candidate-318/362/363/365 与 i18n 后备四处全部落地，全量测试全绿
- [x] wave9+ 新视角扩展：以既有 16 lens 与 mine 之外的新视角产出并落地至少 6 个候选，供给状态从 exhausted 翻新

## 轮次统计

- 已完成: 30
- 受阻: 0
- 已取消: 0
- 已回滚: 0
- 估算 Token: 41700

## 轮次历史

| round | status | title | commit | tokens |
|---|---|---|---|---|
| 32 | 已完成 | 轮33：面板缓存键补 log/时钟（431）+ 回放过滤序（432） | `d1a7402896e3` | 728 |
| 31 | 空转取消 | 轮32：面板缓存键/时钟（432）+ 回放过滤序（433） |  |  |
| 30 | 已完成 | 轮31：426/429 补账落地 + docs-drift 去重键（430） | `1c3f7c2bd498` | 1484 |
| 29 | 已完成 | 轮29（重开）：427/428 落地补账——轮29漏begin坑如实入账 | `71a4d27a8fdb` | 1040 |
| 28 | 已完成 | 轮28：overview 补 mine/round-prep 叙事（424）+ troubleshooting push/diagnose 剧本（425） | `2dc473a5c71e` | 620 |
| 27 | 已完成 | 轮27：mine 枯竭后提示让位扩展（423） | `79d4fb6a2d58` | 836 |
| 26 | 已完成 | 轮26：枯竭判据统一表述（421）+ SKILL 措辞漂移组（422）+ CHANGELOG 增补 | `ca93e965618c` | 1028 |
| 25 | 已完成 | 轮25：边界字段轮号空间（419）+ checkpoint 绊线（420） | `c3c614c9fdc7` | 1256 |
| 24 | 已完成 | 轮24：尾期安全阀保真（416）+ 排序键降位项（418） | `a5ebe3876fc8` | 1304 |
| 23 | 已完成 | 轮23：校准修复落地（414 载体）+ 40% 截断全 below-floor 批次生效（415） | `167099717df5` | 764 |
| 22 | 空转取消 | 轮22：校准搬运落地（414 载体）+ 40% 截断全 below-floor 批次生效（415） |  |  |
| 21 | 已完成 | 轮21：预测校准公式修复（414）+ 目标链归一（417） | `a218084896d7` | 1124 |
| 20 | 已完成 | 轮20：面板 XSS 独苗封堵（412） | `a218084896d7` | 620 |
| 19 | 已完成 | 轮19：帮助串三漂移（409）+ 大小写守卫（411）+ 密钥四模式（413） | `74b6f866ef93` | 1472 |
| 18 | 已完成 | 轮18：拒绝文案行动性（410）+ JSON 契约四处（408） | `e64a268200cf` | 1676 |
| 17 | 已完成 | 轮17：release 1.14.0 | `c4ea4049deac` | 1832 |
| 16 | 已完成 | 轮16：seed/mine 跨文件写序窗口（401）+ 面板增长游走归属（406） | `dc79b33994d9` | 1556 |
| 15 | 已完成 | 轮15：lens 轮换跨窗记忆（403）+ 面板计数器单调化（405） | `d35c3044a7dc` | 1364 |
| 14 | 已完成 | 轮14：detect-verify 加锁（400）+ log 轮转降级保条目（399）+ goal-met 跨窗锚（404） | `10031c3bbac8` | 1448 |
| 13 | 已完成 | 轮13：picked 孤儿自愈（398）+ deferred 跨窗账本（402） | `d8f712c44d5d` | 1376 |
| 12 | 已完成 | 轮12：_unlink_info 异常面扩为 OSError（397） | `1f681544e3e8` | 704 |
| 11 | 已完成 | 轮11：dashboard 3.7+/3.8+ API 降回 3.6 兼容（395） | `e40e81e66a36` | 944 |
| 10 | 已完成 | 轮10：deps 字典化（392）+ finish --json 契约修复（394）+ 文档波次产量示例（396） | `ca72256afb8b` | 1436 |
| 9 | 已完成 | 轮9：commit staged numstat 双查合一（389）+ _close_round 单次读 backlog（391） | `6c922e86c5af` | 1052 |
| 8 | 已完成 | 轮8：troubleshooting 危险恢复指引修正（393）+ check HEAD/analysis 探针合一（390） | `23df124cf769` | 1712 |
| 7 | 已完成 | 轮7：estimate_tokens_for_round 记账契约直测（388） | `b075662ff11d` | 1328 |
| 6 | 已完成 | 轮6：自察三修——remaining 分母对齐（385）+ mining_exhausted 波次回填（386）+ 批宽回退对齐文档（387） | `efa0e44c4e88` | 1316 |
| 5 | 已完成 | 轮5：wave9 尾批——配额 null 语义（381）+ detect-verify 指纹刷新（382）+ state 形状校验补漏（383） | `8e4022b19b5d` | 1328 |
| 4 | 已完成 | 轮4：wave9 三修复——backlog id 碰撞防护（379）+ begin-round status 守卫（380）+ candidates_per_round null 四读者分裂（384） | `33c44e59e405` | 2204 |
| 3 | 已完成 | 轮3：append_log 测试覆盖（377）+ check_view.py 热点复审（378） | `f50d5e09a856` | 1904 |
| 2 | 已完成 | 轮2：i18n 双锚点收尾（362/363）+ config-set 旗标面注册表派生（376） | `e3d97eba43d7` | 1376 |
| 1 | 已完成 | 轮1：config 字段注册表（314）+ commit 守卫 _refuse 助手（318）+ config.md deadline 文档矛盾（365） | `2aeb99730391` | 4868 |

## 改进清单

| id | title | type | value/effort | status |
|---|---|---|---|---|
| `candidate-001` | 封堵 NaN/inf confidence 链路（P1） | bugfix | 5/1 | 已完成 |
| `candidate-002` | below_floor predicted 快赢绕过配额与尾期收紧（B1） | bugfix | 5/2 | 已完成 |
| `candidate-003` | resolve_seed 终态守卫 + 回写归属校验 + 并入 _close_round 单次保存 | bugfix | 4/3 | 已完成 |
| `candidate-004` | 补 rejected 写入路径：seed-reject 命令 | feature | 4/2 | 已完成 |
| `candidate-005` | 截断后 id 单调递增 + seed 字段容错 + OverflowError 补捕获 | bugfix | 3/2 | 已完成 |
| `candidate-006` | Windows 编码包装 + type_stats/backlog 形状校验 | bugfix | 4/3 | 已完成 |
| `candidate-007` | goal-met 去重播种 + commit_shas 回填 + source_event_id 反向链接 | bugfix | 3/2 | 已完成 |
| `candidate-008` | report/retro rank 补 completed_goals + recent_types 防御 + seed-writeback 缺失改 warn | bugfix | 3/2 | 已完成 |
| `candidate-009` | 对抗测试表 + 配置矩阵补项 + 测试基建加固 | test | 5/3 | 已完成 |
| `candidate-010` | 文档五件套同步 1.3.2 + 提案状态更新 | docs | 4/2 | 已完成 |
| `candidate-011` | [P] subagent/agent.py 的 detect-agent 与新 seed 流没有交叉覆盖，补集成测试 | test | 4/2 | 已完成 |
| `candidate-012` | [P] SKILL.md 的 Deep Expansion 16 镜头清单与 helper 能力做一次逐条对表 | feature | 4/2 | 已完成 |
| `candidate-013` | goal-met 文本模式回显 seed id（Agent 在猜 seed-00N） | bugfix | 5/1 | 已完成 |
| `candidate-014` | backlog-add 未 init 时半初始化且 exit 0（吞 SystemExit 连 corrupt state 也 fail-open） | bugfix | 5/1 | 已完成 |
| `candidate-015` | 未知 seed/candidate id 报错补恢复指引 | refactor | 3/1 | 已完成 |
| `candidate-016` | config 指纹变化写审计事件（现在只在 check 警告且无限重复） | bugfix | 4/2 | 已完成 |
| `candidate-017` | state 迁移/回填写 state-migrate 审计事件 | refactor | 4/2 | 已完成 |
| `candidate-018` | analysis-load 判定结果补审计事件 | refactor | 3/1 | 已完成 |
| `candidate-019` | state/backlog 校验失败 SystemExit 前写 integrity 事件 | refactor | 3/1 | 已完成 |
| `candidate-020` | directive-remove 命令（误加的常驻指令只能手改 JSON 且零日志） | refactor | 3/2 | 已完成 |
| `candidate-021` | SKILL init 旗标模板补 --allow-uncommitted-changes/--force | docs | 3/1 | 已完成 |
| `candidate-022` | 测试驱动进程内化：589 次解释器启动是大头（实测 0.33s/次≈195s） | test | 5/3 | 已完成 |
| `candidate-023` | setUp 7 次 git 子进程压到 1-2 次（每测试省 ~0.5s×258） | test | 3/1 | 已完成 |
| `candidate-024` | 热命令冗余 git 调用合并（begin-round 2→1、diagnose 7→5、check 3→2） | perf | 3/2 | 已完成 |
| `candidate-025` | goal 文本归一化：零宽/空白变体让 goal-met 静默假成功，循环永不停止 | bugfix | 5/2 | 已完成 |
| `candidate-026` | 标题/goal 换行与管道符撑破 report 三张表 | bugfix | 5/2 | 已完成 |
| `candidate-027` | directives.json 无形状校验：add/remove 裸 Traceback、list 静默回显垃圾 | bugfix | 4/1 | 已完成 |
| `candidate-028` | current_round 缺 round 键 → 四个关轮命令 KeyError 且 begin-round 拒绝，run 砖化 | bugfix | 4/1 | 已完成 |
| `candidate-029` | history 混入非 dict → check（循环心跳）裸 AttributeError | bugfix | 4/1 | 已完成 |
| `candidate-030` | undo-round 对 merge commit 的指引错误（说冲突可解，实则需 revert -m 1） | bugfix | 3/1 | 已完成 |
| `candidate-031` | backlog-update/pick/remove 缺未初始化守卫（与 add/list/rank 不一致） | bugfix | 3/1 | 已完成 |
| `candidate-032` | report --output 相对路径按 CWD 解析而非 repo（误导报错 + --force 写错位置） | bugfix | 3/1 | 已完成 |
| `candidate-033` | 畸形 --repo 设备路径裸 traceback（~30 个入口，fail-clean 破面） | bugfix | 3/1 | 已完成 |
| `candidate-034` | 空 subject 提交让 recent_commit_topics 混入裸 SHA 进 suggested_themes | bugfix | 2/1 | 已完成 |
| `candidate-035` | guard: deny_paths 反斜杠 pattern 永不匹配（fail-open）+ ** 语义修正 | bugfix | 5/2 | 已完成 |
| `candidate-036` | secrets: PKCS#8 加密私钥与 gho_/ghs_/ghu_/ghr_ token 漏扫 | bugfix | 5/1 | 已完成 |
| `candidate-037` | secrets: sk- 无词边界，task-/risk-/disk- 普通词阻断提交 | bugfix | 4/1 | 已完成 |
| `candidate-038` | parse_deadline 巨大相对时长抛 OverflowError（init 裸崩） | bugfix | 3/1 | 已完成 |
| `candidate-039` | git_push 多 remote 时优先 origin/upstream 而非字母序第一个 | bugfix | 3/1 | 已完成 |
| `candidate-040` | __version__ 单一权威 + --version 旗标 + 五处一致性测试 | refactor | 4/2 | 已完成 |
| `candidate-041` | CI 矩阵与文档 3.6+ 承诺对齐 + timeout/concurrency 护栏 | refactor | 4/2 | 已完成 |
| `candidate-042` | README 安装节补发布物清单与 junction 安装方式 | docs | 3/1 | 已完成 |
| `candidate-043` | openai.yaml 补 description 字段对齐 SKILL 触发词 | docs | 2/1 | 已完成 |
| `candidate-044` | README mklink 示例含 BEL/TAB 控制字符，照抄必失败 | docs | 5/1 | 已完成 |
| `candidate-045` | init 负数旋钮双层穿透：max-blocked-in-a-row 负值零轮静默停机，4 旋钮假成功 | bugfix | 4/2 | 已完成 |
| `candidate-046` | review-score 1-5 校验只在 review_threshold 配置时生效 | bugfix | 3/1 | 已完成 |
| `candidate-047` | depends-on 不校验存在性/自引用（悬空依赖 add 时零警告） | bugfix | 3/1 | 已完成 |
| `candidate-048` | Troubleshooting/命令清单补 v1.3.x 新故障 + directive-list 显示 index | docs | 3/2 | 已完成 |
| `candidate-049` | goal/goals-from-prompt 同用静默覆盖 + batched-mode 措辞矛盾 + 自依赖示例 + 布局缺 3 模块 | docs | 3/2 | 已完成 |
| `candidate-050` | AUTOPILOT_AGENT 非法值静默降级 generic 补 WARN | bugfix | 3/1 | 已完成 |
| `candidate-051` | 测试盲区第一批（P1×5）：merge指引/seed值边界/push无remote --json/dry-run零突变扫描/report相对路径正向 | test | 5/3 | 已完成 |
| `candidate-052` | 测试盲区第二批（P2×3）：未init守卫矩阵/identity门+json/push失败警告三分支 | test | 4/2 | 已完成 |
| `candidate-053` | agent 空串环境信号与注释契约矛盾 + 数值扫描(--round 0/-5、--tokens 负、detect-verify json 漂移) | bugfix | 2/2 | 已完成 |
| `candidate-070` | Stop swallowing exceptions at scripts/autopilot/io.py:172 | bugfix | 4/2 | 已完成 |
| `candidate-073` | Stop swallowing exceptions at scripts/autopilot/miner.py:287 | bugfix | 4/2 | 已完成 |
| `candidate-074` | Stop swallowing exceptions at scripts/autopilot/state.py:150 | bugfix | 4/2 | 已完成 |
| `candidate-075` | Stop swallowing exceptions at scripts/autopilot/state.py:859 | bugfix | 4/2 | 已完成 |
| `candidate-076` | Stop swallowing exceptions at scripts/autopilot/state.py:1728 | bugfix | 4/2 | 已完成 |
| `candidate-077` | Stop swallowing exceptions at scripts/autopilot/verify.py:54 | bugfix | 4/2 | 已完成 |
| `candidate-078` | Cover public functiondef `detect_python` with tests | test | 4/3 | 已完成 |
| `candidate-079` | Cover public functiondef `agent_profile` with tests | test | 4/3 | 已完成 |
| `candidate-080` | Cover public functiondef `cmd_detect_agent` with tests | test | 4/3 | 已完成 |
| `candidate-081` | Cover public functiondef `emit_result` with tests | test | 4/3 | 已完成 |
| `candidate-098` | Review hotspot scripts/test_autopilot_state.py (33 recent commits) | test | 4/3 | 已完成 |
| `candidate-099` | Review hotspot scripts/autopilot/commands.py (28 recent commits) | test | 4/3 | 已完成 |
| `candidate-100` | Review hotspot SKILL.md (23 recent commits) | test | 4/3 | 已完成 |
| `candidate-101` | Review hotspot scripts/autopilot/state.py (20 recent commits) | test | 4/3 | 已完成 |
| `candidate-102` | Review hotspot references/config.md (19 recent commits) | test | 4/3 | 已完成 |
| `candidate-103` | Review hotspot scripts/autopilot/cli.py (19 recent commits) | test | 4/3 | 已完成 |
| `candidate-104` | Review hotspot CHANGELOG.md (18 recent commits) | test | 4/3 | 已完成 |
| `candidate-105` | Review hotspot README.md (18 recent commits) | test | 4/3 | 已完成 |
| `candidate-106` | Review hotspot scripts/autopilot/io.py (13 recent commits) | test | 4/3 | 已完成 |
| `candidate-107` | Review hotspot agents/openai.yaml (12 recent commits) | test | 4/3 | 已完成 |
| `candidate-108` | Review hotspot scripts/autopilot/config.py (9 recent commits) | test | 4/3 | 已完成 |
| `candidate-109` | Review hotspot scripts/autopilot/secrets.py (6 recent commits) | test | 4/3 | 已完成 |
| `candidate-110` | Review hotspot scripts/autopilot/__init__.py (6 recent commits) | test | 4/3 | 已完成 |
| `candidate-111` | Review hotspot scripts/autopilot/agent.py (5 recent commits) | test | 4/3 | 已完成 |
| `candidate-112` | Review hotspot scripts/autopilot_state.py (5 recent commits) | test | 4/3 | 已完成 |
| `candidate-143` | test-gap 扫描器排除 CLI 分发处理器（cli.py func= 注册的符号由 CLI 测试面覆盖） | bugfix | 5/3 | 已完成 |
| `candidate-144` | dead-export 扫描器排除 unittest.TestCase 子类（由测试发现机制引用） | bugfix | 5/2 | 已完成 |
| `candidate-145` | hotspot 扫描器排除已删除路径（git log --name-only 含历史删除文件） | bugfix | 3/1 | 已完成 |
| `candidate-146` | Cover public functiondef `any_ready_candidates` with tests | test | 4/3 | 已完成 |
| `candidate-147` | Cover public functiondef `default_config` with tests | test | 4/3 | 已完成 |
| `candidate-148` | Cover public functiondef `default_backlog` with tests | test | 4/3 | 已完成 |
| `candidate-149` | Cover public functiondef `config_path_for` with tests | test | 4/3 | 已完成 |
| `candidate-150` | Cover public functiondef `state_path_for` with tests | test | 4/3 | 已完成 |
| `candidate-151` | Cover public functiondef `backlog_path_for` with tests | test | 4/3 | 已完成 |
| `candidate-152` | Cover public functiondef `validate_config` with tests | test | 4/3 | 已完成 |
| `candidate-153` | Cover public functiondef `save_config` with tests | test | 4/3 | 已完成 |
| `candidate-154` | Cover public functiondef `now_iso` with tests | test | 4/3 | 已完成 |
| `candidate-155` | Cover public functiondef `load_json` with tests | test | 4/3 | 已完成 |
| `candidate-156` | Cover public functiondef `save_json` with tests | test | 4/3 | 已完成 |
| `candidate-157` | Cover public functiondef `git_dir_for` with tests | test | 4/3 | 已完成 |
| `candidate-158` | Cover public functiondef `branch_exists` with tests | test | 4/3 | 已完成 |
| `candidate-160` | Cover public functiondef `working_tree_dirty` with tests | test | 4/3 | 已完成 |
| `candidate-161` | Cover public functiondef `uncommitted_paths` with tests | test | 4/3 | 已完成 |
| `candidate-162` | Cover public functiondef `git_identity_ok` with tests | test | 4/3 | 已完成 |
| `candidate-163` | Cover public functiondef `lock_path_for` with tests | test | 4/3 | 已完成 |
| `candidate-164` | Cover public functiondef `run_lock` with tests | test | 4/3 | 已完成 |
| `candidate-165` | Remove or wire up unused public symbol `is_detached_head` | refactor | 3/2 | 已完成 |
| `candidate-166` | Remove or wire up unused public symbol `update_candidate_status` | refactor | 3/2 | 已完成 |
| `candidate-167` | config-set 支持常用运行时字段（candidates_per_round/commit_every_rounds/verify_every_rounds/max_rounds/deadline/max_minutes/push/scan_secrets/report_lang）并刷新 config 指纹 | feature | 4/3 | 已完成 |
| `candidate-168` | markers/swallowed 扫描器跳过测试路径（夹具字符串污染供给） | bugfix | 4/1 | 已完成 |
| `candidate-169` | diagnose 非 git 目录时不触发 [ERROR] 前缀（诊断命令 exit 0 语义） | bugfix | 3/1 | 已完成 |
| `candidate-170` | CLI 参数名别名一致性：backlog-add --text/--directive、backlog-remove --id/--candidate-id 等高频误用参数补别名 | feature | 4/1 | 已完成 |
| `candidate-171` | init --secret-pattern 非法正则报错区分来源（命令行 vs 磁盘 config.json） | bugfix | 3/1 | 已完成 |
| `candidate-172` | 锁获取的 corrupt 分支增加宽限重试（创建与写 payload 间的竞态窗口） | bugfix | 3/2 | 已完成 |
| `candidate-173` | check 命令合并重复的 current_branch git 子进程（U-18） | perf | 3/1 | 已完成 |
| `candidate-174` | save_state 写前保留上一份完好状态备份（state.json.bak） | feature | 4/2 | 已完成 |
| `candidate-176` | exhausted 判定只计 --apply 的 mining run（只读探测污染序列） | bugfix | 4/1 | 已完成 |
| `candidate-177` | Resolve TODO at scripts/autopilot/miner.py:65 | refactor | 3/2 | 已完成 |
| `candidate-178` | Resolve TODO at scripts/autopilot/miner.py:67 | refactor | 3/2 | 已完成 |
| `candidate-179` | Resolve TODO at scripts/autopilot/miner.py:148 | refactor | 3/2 | 已完成 |
| `candidate-181` | Stop swallowing exceptions at scripts/autopilot/dashboard.py:66 | bugfix | 4/2 | 已完成 |
| `candidate-182` | Stop swallowing exceptions at scripts/autopilot/dashboard.py:243 | bugfix | 4/2 | 已完成 |
| `candidate-183` | Stop swallowing exceptions at scripts/autopilot/miner.py:191 | bugfix | 4/2 | 已完成 |
| `candidate-184` | Stop swallowing exceptions at scripts/autopilot/miner.py:191 | bugfix | 4/2 | 已完成 |
| `candidate-185` | Cover public functiondef `brief_rank_entry` with tests | test | 4/3 | 已完成 |
| `candidate-186` | Cover public functiondef `build_check_payload` with tests | test | 4/3 | 已完成 |
| `candidate-187` | Cover public functiondef `spawn_server` with tests | test | 4/3 | 已完成 |
| `candidate-188` | Cover public functiondef `get_snapshot` with tests | test | 4/3 | 已完成 |
| `candidate-189` | Cover public functiondef `worktree_units` with tests | test | 4/3 | 已完成 |
| `candidate-190` | Cover public functiondef `worktree_change_lines` with tests | test | 4/3 | 已完成 |
| `candidate-191` | Cover public functiondef `ensure_git_exclude` with tests | test | 4/3 | 已完成 |
| `candidate-192` | Cover public functiondef `autopilot_file_for` with tests | test | 4/3 | 已完成 |
| `candidate-198` | Cover public functiondef `scan_hotspot` with tests | test | 4/3 | 已完成 |
| `candidate-199` | Cover public functiondef `scan_dead_export` with tests | test | 4/3 | 已完成 |
| `candidate-200` | Cover public functiondef `scan_docs_drift` with tests | test | 4/3 | 已完成 |
| `candidate-201` | Cover public functiondef `mine_repo` with tests | test | 4/3 | 已完成 |
| `candidate-202` | Cover public functiondef `filter_new_findings` with tests | test | 4/3 | 已完成 |
| `candidate-203` | Cover public functiondef `finding_to_candidate_fields` with tests | test | 4/3 | 已完成 |
| `candidate-205` | Review hotspot scripts/autopilot/dashboard_data.py (7 recent commits) | test | 4/3 | 已完成 |
| `candidate-206` | Review hotspot scripts/autopilot/dashboard.html (4 recent commits) | refactor | 3/3 | 已完成 |
| `candidate-207` | Review hotspot references/troubleshooting.md (4 recent commits) | refactor | 3/3 | 已完成 |
| `candidate-208` | Review hotspot scripts/autopilot/dashboard.py (3 recent commits) | refactor | 3/3 | 已完成 |
| `candidate-209` | Review hotspot references/overview.md (3 recent commits) | refactor | 3/3 | 已完成 |
| `candidate-210` | Review hotspot .gitignore (2 recent commits) | refactor | 3/3 | 已完成 |
| `candidate-211` | Review hotspot scripts/autopilot/verify.py (2 recent commits) | refactor | 3/3 | 已完成 |
| `candidate-212` | Review hotspot docs/README.legacy.md (2 recent commits) | refactor | 3/3 | 已完成 |
| `candidate-213` | 测试分层：新增 smoke 快速验证入口，轮间验证摆脱 7-8 分钟全量 | perf | 5/3 | 已完成 |
| `candidate-214` | 批量提交 run 下生长树粗粒度回退：run_start_sha..HEAD 聚合代替整体降级 | feature | 5/3 | 已完成 |
| `candidate-215` | [P] smoke 对账：超时自检——_run_smoke 实测超 60s 时提示慢类清单漂移 | refactor | 3/2 | 已完成 |
| `candidate-216` | 观察台补齐「迭代方向」板块：右栏展示 pending 候选清单（价值排序） | feature | 4/3 | 已完成 |
| `candidate-217` | 发布 1.9.0：版本六处 bump + CHANGELOG 段 | docs | 5/2 | 已完成 |
| `candidate-218` | [P] 收轮记录 touched_files：批量模式下快照恢复每轮域归因 | feature | 4/3 | 已完成 |
| `candidate-223` | Cover public functiondef `compute_touched_file_changes` with tests | test | 4/3 | 已完成 |
| `candidate-224` | Cover public functiondef `compute_run_file_changes` with tests | test | 4/3 | 已完成 |
| `candidate-228` | Cover public functiondef `mask_secret_text` with tests | test | 4/3 | 已完成 |
| `candidate-229` | Cover public functiondef `normalize_goal_text` with tests | test | 4/3 | 已完成 |
| `candidate-230` | Cover public functiondef `table_cell` with tests | test | 4/3 | 已完成 |
| `candidate-231` | Cover public functiondef `default_state` with tests | test | 4/3 | 已完成 |
| `candidate-232` | Cover public functiondef `migrate_state` with tests | test | 4/3 | 已完成 |
| `candidate-233` | Cover public functiondef `load_backlog` with tests | test | 4/3 | 已完成 |
| `candidate-234` | Cover public functiondef `save_backlog` with tests | test | 4/3 | 已完成 |
| `candidate-235` | Cover public functiondef `find_candidate` with tests | test | 4/3 | 已完成 |
| `candidate-236` | Cover public functiondef `update_candidates_status` with tests | test | 4/3 | 已完成 |
| `candidate-237` | Cover public functiondef `round_candidate_ids` with tests | test | 4/3 | 已完成 |
| `candidate-238` | Cover public functiondef `find_seed` with tests | test | 4/3 | 已完成 |
| `candidate-239` | Cover public functiondef `open_seeds` with tests | test | 4/3 | 已完成 |
| `candidate-240` | Cover public functiondef `append_goal_event` with tests | test | 4/3 | 已完成 |
| `candidate-241` | Cover public functiondef `append_seed` with tests | test | 4/3 | 已完成 |
| `candidate-242` | Cover public functiondef `count_consecutive_blocked` with tests | test | 4/3 | 已完成 |
| `candidate-243` | Cover public functiondef `unverified_goals` with tests | test | 4/3 | 已完成 |
| `candidate-245` | Cover public functiondef `progress_from_state` with tests | test | 4/3 | 已完成 |
| `candidate-246` | Cover public functiondef `analysis_path_for` with tests | test | 4/3 | 已完成 |
| `candidate-247` | Cover public functiondef `directives_path_for` with tests | test | 4/3 | 已完成 |
| `candidate-248` | Cover public functiondef `load_directives` with tests | test | 4/3 | 已完成 |
| `candidate-249` | Cover public functiondef `save_directives` with tests | test | 4/3 | 已完成 |
| `candidate-250` | Cover public functiondef `add_directive` with tests | test | 4/3 | 已完成 |
| `candidate-251` | Cover public functiondef `load_analysis` with tests | test | 4/3 | 已完成 |
| `candidate-252` | Cover public functiondef `save_analysis` with tests | test | 4/3 | 已完成 |
| `candidate-253` | Cover public functiondef `config_mtime` with tests | test | 4/3 | 已完成 |
| `candidate-254` | Cover public functiondef `analysis_validity` with tests | test | 4/3 | 已完成 |
| `candidate-255` | Cover public functiondef `compile_patterns` with tests | test | 4/3 | 已完成 |
| `candidate-256` | Cover public functiondef `analysis_commits_behind` with tests | test | 4/3 | 已完成 |
| `candidate-257` | Cover public functiondef `build_retrospective` with tests | test | 4/3 | 已完成 |
| `candidate-258` | Cover public functiondef `build_report` with tests | test | 4/3 | 已完成 |
| `candidate-259` | Cover public functiondef `write_phase_report` with tests | test | 4/3 | 已完成 |
| `candidate-260` | Cover public functiondef `detect_verify_commands` with tests | test | 4/3 | 已完成 |
| `candidate-261` | commit 批次回填：flush 时把批次 sha 记入该批各轮 history 条目（batch_commit_sha） | feature | 4/3 | 已完成 |
| `candidate-262` | 观察台成果回顾板块接入 retrospective 摘要（run 结束后可读） | feature | 3/2 | 已完成 |
| `candidate-264` | 全量验证并行化：慢类清单按类并行跑（multiprocessing 池），全量时间目标 455s→<180s | perf | 5/4 | 已完成 |
| `candidate-265` | dashboard.json 原子写：write_info 改临时文件+os.replace，防半写文件 | refactor | 2/1 | 已完成 |
| `candidate-266` | finish --archive-reports：把 phase-report/retrospective 归档进仓库 docs/（默认关闭） | feature | 3/2 | 已完成 |
| `candidate-267` | observability：快照暴露最近运行事件流 | feature | 3/2 | 已完成 |
| `candidate-268` | backlog-add 在扩展阶段缺 --evidence 时输出 WARN 提醒 | feature | 4/1 | 已完成 |
| `candidate-269` | mine --apply 向 log.jsonl 写审计事件 | feature | 4/1 | 已完成 |
| `candidate-270` | block-round 事件补 title、候选补 block_reason 字段 | feature | 4/2 | 已完成 |
| `candidate-271` | block-round 输出回显被 block 的候选清单 | feature | 3/1 | 已完成 |
| `candidate-272` | expansion-record 增加每波产出统计（findings/applied 数） | feature | 3/3 | 已完成 |
| `candidate-273` | finish 报告/last-summary 列 pending 候选交接清单 | feature | 3/1 | 已完成 |
| `candidate-274` | SKILL.md Deep Expansion 补「疑似不可行候选单轮隔离」指引 | docs | 3/1 | 已完成 |
| `candidate-275` | 测试 runner dispatch 修复：--smoke 排在 --jobs 前被静默忽略 | bugfix | 4/1 | 已完成 |
| `candidate-276` | Stop swallowing exceptions at scripts/autopilot/commands.py:361 | bugfix | 4/2 | 已完成 |
| `candidate-277` | Stop swallowing exceptions at scripts/autopilot/commands.py:1830 | bugfix | 4/2 | 已完成 |
| `candidate-278` | Stop swallowing exceptions at scripts/autopilot/state.py:629 | bugfix | 4/2 | 已完成 |
| `candidate-279` | Cover public functiondef `detect_shell` with tests | test | 4/3 | 已完成 |
| `candidate-280` | Cover public functiondef `skill_root` with tests | test | 4/3 | 已完成 |
| `candidate-281` | Cover public functiondef `mining_exhausted` with tests | test | 4/3 | 已完成 |
| `candidate-282` | Cover public functiondef `read_info` with tests | test | 4/3 | 已完成 |
| `candidate-283` | Cover public functiondef `clear_stale_info` with tests | test | 4/3 | 已完成 |
| `candidate-284` | Cover public functiondef `stop_server` with tests | test | 4/3 | 已完成 |
| `candidate-285` | Cover public functiondef `invalidate_snapshot_cache` with tests | test | 4/3 | 已完成 |
| `candidate-286` | Cover public functiondef `compute_round_file_changes` with tests | test | 4/3 | 已完成 |
| `candidate-287` | Review hotspot README.zh-CN.md (8 recent commits) | test | 4/3 | 已完成 |
| `candidate-288` | Review hotspot .github/workflows/ci.yml (2 recent commits) | refactor | 3/3 | 已完成 |
| `candidate-289` | Cover public functiondef `compute_round_stats` with tests | test | 4/3 | 已完成 |
| `candidate-290` | Cover public functiondef `aggregate_modules` with tests | test | 4/3 | 已完成 |
| `candidate-291` | Cover public functiondef `correlate_events` with tests | test | 4/3 | 已完成 |
| `candidate-292` | Cover public functiondef `current_branch` with tests | test | 4/3 | 已完成 |
| `candidate-293` | Cover public functiondef `has_commits` with tests | test | 4/3 | 已完成 |
| `candidate-294` | report --json 未初始化时破坏 JSON 单对象契约 | bugfix | 3/1 | 已完成 |
| `candidate-295` | backlog-pick 缺状态迁移守卫与 type_stats 同步 | bugfix | 3/2 | 已完成 |
| `candidate-296` | finish/init 的 --dry-run 在拒绝门之前返回 0 | bugfix | 3/2 | 已完成 |
| `candidate-297` | token 估算把失败的 git diff --numstat 静默当 0 行 | bugfix | 4/2 | 已完成 |
| `candidate-298` | 吞异常治理三处漏网：init project_map/init branch 清理/_close_round deferred 记录 | refactor | 3/1 | 已完成 |
| `candidate-299` | cmd_commit 成功后收尾前的失败窗口丢 batch_commit_sha 回填与审计 | bugfix | 2/2 | 已完成 |
| `candidate-300` | round-prep 性能：HEAD 三重解析与上下文整体重算 | perf | 3/2 | 已完成 |
| `candidate-301` | 同命令内重复的 git status --porcelain 调用 | perf | 2/1 | 已完成 |
| `candidate-302` | dashboard 快照管道：TTL 短路前置 + 单遍 git log 取全量 numstat | perf | 4/3 | 已完成 |
| `candidate-303` | check/round-prep 循环决策写 log.jsonl 事件 | feature | 4/2 | 已完成 |
| `candidate-304` | dashboard 静默失败两处：ensure 异常吞没与 _loop_outlook 降级不进 degraded | bugfix | 3/1 | 已完成 |
| `candidate-305` | begin-round 成功事件补 title/start_sha 字段 | feature | 3/1 | 已完成 |
| `candidate-306` | 锁互斥击穿：grace 只护第 0 次尝试 + 释放不校验归属 | bugfix | 4/2 | 已完成 |
| `candidate-307` | read 类命令锁外 load_state 触发迁移写，可与活跃 run 丢更新 | bugfix | 4/2 | 已完成 |
| `candidate-308` | secret_patterns 用户自供模式无 ReDoS 防护 | feature | 3/1 | 已完成 |
| `candidate-309` | report/retrospective --output 可覆写 .autopilot/ 运行态 | bugfix | 3/1 | 已完成 |
| `candidate-310` | backlog.json 加同款 .bak 单代备份 | feature | 3/1 | 已完成 |
| `candidate-311` | .bak 备份自身改为原子写（tmp+fsync+replace） | refactor | 2/1 | 已完成 |
| `candidate-312` | dashboard write_info 固定名 tmp + ensure 锁外并发双 spawn | bugfix | 3/1 | 已完成 |
| `candidate-313` | _snapshot_cache 单槽全局在 ThreadingHTTPServer 下无锁读写 | bugfix | 2/1 | 已完成 |
| `candidate-314` | config 字段六处手工同步面收敛为单一注册表 | refactor | 3/3 | 已完成 |
| `candidate-315` | 统一包内导入接缝（绝对→相对）作为 check 模块拆分前置 | refactor | 2/1 | 已完成 |
| `candidate-316` | 从 commands.py 抽出 check/loop 决策视图纯函数模块 | refactor | 3/3 | 已完成 |
| `candidate-317` | rank_candidates 调用+选中批次提取四处手写收敛为单一 helper | refactor | 3/3 | 已完成 |
| `candidate-318` | cmd_commit 守卫链抽 _refuse 助手（12 组审计+拒绝三元组） | refactor | 3/2 | 已完成 |
| `candidate-319` | 八处内联未初始化守卫统一走 _require_initialized | refactor | 2/1 | 已完成 |
| `candidate-320` | scan_hotspot 的 limit 参数被忽略——违反 config.md 的 per-kind 上限承诺 | bugfix | 3/1 | 已完成 |
| `candidate-321` | 死参数清理：_config_error 的 path 与 _risk_weight_for 的 cfg | refactor | 2/1 | 已完成 |
| `candidate-322` | cmd_directive_list 末尾不可达的重复 return 0 | refactor | 2/1 | 已完成 |
| `candidate-323` | 版本一致性测试补 README.zh-CN.md | test | 3/1 | 已完成 |
| `candidate-324` | lens 文档表与 EXPANSION_LENSES 常量同步测试 | test | 3/1 | 已完成 |
| `candidate-325` | 子命令层支持 --version | feature | 2/1 | 已完成 |
| `candidate-326` | 四处用户文案补 report_lang 双语门控 | bugfix | 3/2 | 已完成 |
| `candidate-327` | SKILL.md 排查表 312 行三列错位修正 | docs | 2/1 | 已完成 |
| `candidate-328` | retrospective --lang 补 help 文本（与 report 同族） | docs | 2/1 | 已完成 |
| `candidate-329` | finish 拒绝门（含 dry-run 孪生）补 report_lang 双语 | bugfix | 4/1 | 已完成 |
| `candidate-330` | check_view needs_expansion 三分支+wave cap/blocked streak 警告补双语 | bugfix | 4/1 | 已完成 |
| `candidate-331` | review_threshold 硬拒绝与预告警告补双语 | bugfix | 3/1 | 已完成 |
| `candidate-332` | 二进制文件 token 计费项补断言 | test | 3/1 | 已完成 |
| `candidate-333` | 「总缩小不降水位线」金样测试 | test | 3/1 | 已完成 |
| `candidate-334` | dashboard 改名 numstat 解析（rename 四形态）金样测试 | test | 3/1 | 已完成 |
| `candidate-335` | 两份 README 的 ~190 tests 计数漂移修正与去数字化 | docs | 2/1 | 已完成 |
| `candidate-336` | SKILL.md 补 finish --archive-reports | docs | 3/1 | 已完成 |
| `candidate-337` | config.md 命令节「自动写中文阶段报告」与 report_lang 矛盾修正 | docs | 2/1 | 已完成 |
| `candidate-338` | Cover public functiondef `rank_for_selection` with tests | test | 4/3 | 已完成 |
| `candidate-339` | backlog-rank 的 [INFO] 截断提示移到 stderr | bugfix | 3/1 | 已完成 |
| `candidate-340` | token 估算的 staged-delete 双计边缘用测试钉住 | test | 2/1 | 已完成 |
| `candidate-341` | backlog_watch 纯契约直测（pending/ready/needs_expansion 边界） | test | 3/1 | 已完成 |
| `candidate-342` | validate_config deadline 分支引用未定义 repo——护栏自己崩成 NameError | bugfix | 4/1 | 已完成 |
| `candidate-343` | goals 只查 list 不查项类型——非字符串目标成永远无法满足的死目标 | bugfix | 3/1 | 已完成 |
| `candidate-344` | max_blocked_in_a_row 补 config-set 旗标或文档化排除理由 | feature | 3/2 | 已完成 |
| `candidate-345` | check 决策审计事件补 log.jsonl 断言 | test | 3/1 | 已完成 |
| `candidate-346` | _create_lock_exclusive token 写入失败路径补覆盖 | test | 3/1 | 已完成 |
| `candidate-347` | 六处 _check_text 的 en 分支补双跑覆盖 | test | 3/1 | 已完成 |
| `candidate-348` | finish 拒绝门（含 dry-run 孪生）补 report_lang 双语 | bugfix | 4/1 | 已完成 |
| `candidate-349` | check_view needs_expansion 三分支+wave cap/blocked streak 警告补双语 | bugfix | 4/1 | 已完成 |
| `candidate-350` | review_threshold 硬拒绝与预告警告补双语 | bugfix | 3/1 | 已完成 |
| `candidate-351` | max_blocked 0 语义三处失真：新 help/config.md 与实现相反 + config-set 拒 0 与 init 不一致 | bugfix | 3/1 | 已完成 |
| `candidate-352` | config.md max_blocked 字段节补配置入口与真实 0 语义 | docs | 2/1 | 已完成 |
| `candidate-353` | deadline 校验错误补 config-set --deadline 轻量修复路径 | docs | 2/1 | 已完成 |
| `candidate-354` | max_blocked 0 语义三处修正（cli help/config-set 非负门/config.md 勘误） | bugfix | 3/1 | 已完成 |
| `candidate-355` | config.md max_blocked 字段节补配置入口 | docs | 2/1 | 已完成 |
| `candidate-356` | deadline 校验错误补 config-set --deadline 轻量修复路径 | docs | 2/1 | 已完成 |
| `candidate-357` | ReDoS 探测误杀合法宽匹配模式（assert not match 过激） | bugfix | 4/1 | 已完成 |
| `candidate-358` | ReDoS 探测无记忆化且 init/config-set 不预检 | feature | 3/1 | 已完成 |
| `candidate-359` | 锁 corrupt 分支 stat() 与并发 unlink 竞态裸崩 | bugfix | 4/1 | 已完成 |
| `candidate-360` | 锁 stale 分支 unlink 失败零退避 + 「after retrying」误报 | refactor | 2/1 | 已完成 |
| `candidate-361` | _release_lock TOCTOU 残余窗口文档化 | docs | 2/1 | 已完成 |
| `candidate-362` | begin-round 目标达成拒绝 zh 分支补中文锚点统一三风格 | refactor | 2/1 | 已完成 |
| `candidate-363` | backlog-pick 账本防护拒绝补双语与下一步指引 | refactor | 2/1 | 已完成 |
| `candidate-364` | 四处新增双语串的 en 分支测试守卫 | test | 3/1 | 已完成 |
| `candidate-365` | config.md 相对 deadline 表达式描述与实现矛盾 | docs | 3/1 | 已完成 |
| `candidate-366` | 决策审计事件区分 check/round-prep 来源 + config.md 审计面收录 | bugfix | 3/1 | 已完成 |
| `candidate-367` | 面板渲染 loop-outlook 降级标记 | feature | 2/1 | 已完成 |
| `candidate-368` | backlog-pick zh 拒绝语病（已 已完成）与缺下一步指引 | refactor | 2/1 | 已完成 |
| `candidate-369` | begin-round 兜底停止拒绝补双语（与 goals-met 分支对称） | refactor | 2/1 | 已完成 |
| `candidate-370` | _pattern_compiles_in_time docstring 被记忆化插入挤成死串 | docs | 2/1 | 已完成 |
| `candidate-371` | _release_lock TOCTOU docstring「Narrowed by re-reading below」自相矛盾 | docs | 2/1 | 已完成 |
| `candidate-372` | config.md deadline 表引导句补 config-set 入口 | docs | 2/1 | 已完成 |
| `candidate-373` | 决策审计事件 via 字段补断言 | test | 3/1 | 已完成 |
| `candidate-374` | ReDoS 探测补「命中但快」阳性用例 | test | 3/1 | 已完成 |
| `candidate-375` | 锁 corrupt 分支 stat 竞态守卫补回归测试 | test | 3/1 | 已完成 |
| `candidate-376` | [P] init/config-set argparse 字段面（cli.py 手写旗标）从注册表派生，彻底关掉六处同步面剩余两处 | refactor | 3/4 | 已完成 |
| `candidate-377` | Cover public functiondef `append_log` with tests | test | 4/3 | 已完成 |
| `candidate-378` | Review hotspot scripts/autopilot/check_view.py (3 recent commits) | refactor | 3/3 | 已完成 |
| `candidate-379` | backlog next_id/重复 id 防护：碰撞静默改写完成账本 | bugfix | 4/2 | 已完成 |
| `candidate-380` | begin-round 缺 status 守卫：--candidate-id 指向已完成候选静默双记账 | bugfix | 3/1 | 已完成 |
| `candidate-381` | max_same_type_per_round null 不禁用多样性配额：文档承诺与实现相反 | bugfix | 3/1 | 已完成 |
| `candidate-382` | detect-verify --apply 不刷新 config_fingerprint：永久假漂移警报 | bugfix | 3/1 | 已完成 |
| `candidate-383` | mining_runs/expansion_waves 形状损坏致 mine/expansion-record 裸 AttributeError | bugfix | 3/2 | 已完成 |
| `candidate-384` | candidates_per_round 显式 null 过验证但四读者分裂（3/1/1/5） | bugfix | 2/2 | 已完成 |
| `candidate-385` | check 的 round_progress.remaining 用 round_seq 作分母：有取消轮时提前假归零 | bugfix | 3/1 | 已完成 |
| `candidate-386` | mining_exhausted 的波次项读恒 0 的 added：回填使枯竭判据与 SKILL 契约一致 | bugfix | 3/2 | 已完成 |
| `candidate-387` | _mark_selection 批宽回退 3 与文档默认 4 不一致 | refactor | 2/1 | 已完成 |
| `candidate-388` | Cover public functiondef `estimate_tokens_for_round` with tests | test | 4/3 | 已完成 |
| `candidate-389` | cmd_commit 共享 staged numstat 快照：secrets 二进制探测与 scope 计数各 spawn 一次同查询 | perf | 3/2 | 已完成 |
| `candidate-390` | check/round-prep HEAD 三探针与 analysis.json 重复解析合一 | perf | 4/2 | 已完成 |
| `candidate-391` | _close_round 单次读 backlog：update_candidates_status 回读刚写文件再喂 type_stats | refactor | 3/2 | 已完成 |
| `candidate-392` | candidate_deps_status 线性扫描依赖改字典查找 | perf | 2/1 | 已完成 |
| `candidate-393` | troubleshooting.md 用 init --force 做运行中恢复：既清空 run 又修不好脏树拒绝 | docs | 5/1 | 已完成 |
| `candidate-394` | finish --json 四处信息行直上 stdout：破坏单 JSON 对象契约 | bugfix | 2/1 | 已完成 |
| `candidate-395` | dashboard 用 3.7+/3.8+ API 打破 README 的 Python 3.6+ 承诺 | bugfix | 2/2 | 已完成 |
| `candidate-396` | 文档示例从不示范 expansion-record --findings/--applied：波次产量统计恒 null | docs | 2/1 | 已完成 |
| `candidate-397` | Stop swallowing exceptions at scripts/autopilot/dashboard.py:98 | bugfix | 4/2 | 已完成 |
| `candidate-398` | begin-round 入口自动复位 picked 孤儿：崩溃残留撞上反双拾守卫成死锁 | bugfix | 4/2 | 已完成 |
| `candidate-399` | log 轮转撞打开句柄即整条审计丢失：持久读者下 5MB 后全丢 | bugfix | 3/2 | 已完成 |
| `candidate-400` | detect-verify --apply 是唯一无 run_lock 的状态写者 | bugfix | 3/1 | 已完成 |
| `candidate-401` | seed 晋升与 mine 记账的跨文件写序窗口 | bugfix | 2/2 | 已完成 |
| `candidate-402` | deferred_dirty_files 窗口并集遗忘：百轮后 run 自产遗留被误标用户改动而不可提交 | bugfix | 4/3 | 已完成 |
| `candidate-403` | lens 轮换失忆：20 波窗口滑动后 lenses_unused 重报已用镜头、wave_no 冻结 | bugfix | 3/2 | 已完成 |
| `candidate-404` | goal-met --round 修剪后拒真锚：目标停止条件永久失效 | bugfix | 3/2 | 已完成 |
| `candidate-405` | 面板三计数器窗口封顶：completed_rounds 停 100、expansion_waves 停 20、remaining 沿用 round_seq | bugfix | 3/2 | 已完成 |
| `candidate-406` | 面板增长游走把窗口外轮的变更量记到首个窗口内轮 | bugfix | 2/2 | 已完成 |
| `candidate-407` | release 1.14.0：CHANGELOG 轮 1-16 全量 + 版本三处同步 | docs | 4/2 | 已完成 |
| `candidate-408` | JSON 载荷违反文档 {ok,message} 契约四处：secret-scan 无 ok、block-round 候选只在散文里 | bugfix | 4/2 | 已完成 |
| `candidate-409` | 帮助串与实现行为三处漂移：--confidence 默认、--round 上界、max_rounds/max_minutes 域不一致 | docs | 3/2 | 已完成 |
| `candidate-410` | 拒绝文案补「下一步命令」：push 拒绝指向手编 config 而非 config-set --push 等 7 处 | docs | 4/2 | 已完成 |
| `candidate-411` | .autopilot/ 运行态保护大小写敏感：macOS 大小写不敏感盘上可被 case 玩法绕过 | bugfix | 3/1 | 已完成 |
| `candidate-412` | 面板 XSS 独苗：候选 value/effort 走 innerHTML 未过 escapeHtml | bugfix | 2/1 | 已完成 |
| `candidate-413` | 密钥扫描四缺口：Slack webhook、GitLab PAT、Google OAuth secret、PyPI/npm token | bugfix | 3/2 | 已完成 |
| `candidate-414` | 预测校准除以完成次数而非平均价值：管线越成功折扣越狠 | bugfix | 4/2 | 已完成 |
| `candidate-415` | 全 below-floor 批次 40% 截断永不生效（cutoff_base 恒 None） | bugfix | 3/2 | 已完成 |
| `candidate-416` | 尾期 predicted 豁免：observed ready 全 below-floor 时安全阀失效 | bugfix | 3/2 | 已完成 |
| `candidate-417` | 目标链加成跳过 normalize_goal_text：NFC/零宽/空白差异吞掉 ≤1.08 加成 | bugfix | 2/1 | 已完成 |
| `candidate-418` | 排序键缺 below-floor 降位项：同分时快赢靠 id 反超主池 | bugfix | 2/1 | 已完成 |
| `candidate-419` | verify/commit/checkpoint 边界字段 aborted 后与真实轮号脱钩 | bugfix | 3/2 | 已完成 |
| `candidate-420` | checkpoint_every 零代码支持：边界时刻无警告无拒绝无提醒 | bugfix | 3/2 | 已完成 |
| `candidate-421` | SKILL.md 枯竭判据两处矛盾表述与代码合取不一致 | docs | 3/2 | 已完成 |
| `candidate-422` | SKILL.md 措辞漂移组：停止条件次序/dashboard 只读表述/逐候选单元纯约定 | docs | 2/1 | 已完成 |
| `candidate-423` | mine 枯竭后 action_hint 卡死在 mine：exhausted 应让位 Deep Expansion 提示 | bugfix | 3/1 | 已完成 |
| `candidate-424` | overview.md 从不提 mine/round-prep：只读总览的 agent 会先走判断扩展反模式 | docs | 4/2 | 已完成 |
| `candidate-425` | troubleshooting 缺 push 失败剧本且从不指路 diagnose | docs | 3/2 | 已完成 |
| `candidate-426` | detect-agent 家目录探针查错文件：~/.CLAUDE.md 而非 ~/.claude/CLAUDE.md | bugfix | 3/1 | 待处理 |
| `candidate-427` | scan_swallowed 命中自家 docstring：每次 mine 产两条垃圾 v4 候选 | bugfix | 4/2 | 已完成 |
| `candidate-428` | detect-verify 漏 scripts/test_*.py 布局：对本仓库返回空 | bugfix | 4/2 | 已完成 |
| `candidate-429` | smoke 建议不查 lint 脚本存在：npm run lint 记入后每轮红 | bugfix | 3/1 | 待处理 |
| `candidate-430` | docs-drift 去重键只按文件：同一 README 的 N 条断链只报第一条 | bugfix | 2/1 | 已完成 |
| `candidate-431` | 面板快照缓存键漏 log.jsonl 与时钟：运行事件冻结、倒计时停走 | bugfix | 4/2 | 已完成 |
| `candidate-432` | 回放先切片后过滤：早期轮回放整条时间线空白 | bugfix | 4/2 | 已完成 |
| `candidate-433` | stop_server 无探活即 taskkill：PID 重用误杀无辜进程 | bugfix | 3/1 | 待处理 |

## 方向假设（种子）

| id | status | type | title | source goal |
|---|---|---|---|---|
| `seed-001` | 已验证 | refactor | init/config-set argparse 字段面（cli.py 手写旗标）从注册表派生，彻底关掉六处同步面剩余两处 | 完成 config 字段注册表重构（candidate-314）：config-set 与字段校验统一注册表驱动，消除散落分支，全量测试全绿 |

- 预测子账：已完成 5 / 受阻 0（受阻率 0%）

## 最近提交

```
d1a7402 autopilot(round-32): 轮33：面板快照键补 log.jsonl+30s 时钟桶（431，轮中事件流与倒计时不再冻结）；回放先过滤后切片（432，早期轮回放不再整线空白，索引保持全局）；全量 322 项全绿
1c3f7c2 autopilot(round-30): 轮31（2/2）：docs-drift 去重键按断链独立（430，evidence 入键）；CHANGELOG 增补 wave14 供给面/提示面/文档面各项；全量 322 项全绿
eb0a202 autopilot(round-30): 轮31（1/2）：426/429 补账落地（detect-agent 规范家目录探针 + smoke 建议先读 scripts 键，双实机复验）
71a4d27 autopilot(round-29): 轮29：scan_swallowed 自指守卫（427）+ detect-verify 全树测试发现（428，本仓库实机从空到检出 pytest）；漏 begin 坑复现一次（complete 后直投工作），stash 往返补救如实入账；全量 319 项全绿
2dc473a autopilot(round-28): 轮28：overview 补 mine/round-prep 叙事与循环步（424，agent-facing 总览不再漏掉第一供给步）；troubleshooting 补 push 失败/diagnose/undo 合并提交/ensure-branch 脏树/批量冲刷五剧本（425）；全量 317 项全绿
79d4fb6 autopilot(round-27): 轮27：mine 枯竭后提示让位扩展（423，exhausted 跳过 mine 降级；本 run 实况为证——连续零新 exhausted=true 而提示仍教跑注定零产的 mine）；全量 317 项全绿
ca93e96 autopilot(round-26): 轮26：SKILL.md 枯竭判据统一为代码合取式并标注 agent 侧诚实条（421）；措辞漂移组五处对齐（422，停止条件次序/dashboard 只读精确化/逐候选单元标注/）；CHANGELOG 增补轮 18-25 十三项；全量 316 项全绿
c3c614c autopilot(round-25): 轮25：边界字段改轮号空间锚（419，max(计数器,round_seq)+1 兼容两种状态形态，aborted 脱钩修复且旧锚测试形态保持绿）；checkpoint 绊线（420，下一轮撞界时双语警告，补齐姊妹节奏都在场的在环提醒）；全量 316 项全绿
a5ebe38 autopilot(round-24): 轮24：尾期安全阀覆盖 below-floor observed（416，ready_pending 全集口径）；排序键提取 _rank_sort_key 并补同分降位项（418，键级断言双锚：同分快赢居后、分数仍主导）；全量 314 项全绿
1670997 autopilot(round-23): 轮23（2/2）：40% 截断在全 below-floor 批次生效（415，backfill 首选定基 + cut_reason 标注对齐池内循环）；全量 312 项全绿
```

## 下一步建议

- candidate-426 `detect-agent 家目录探针查错文件：~/.CLAUDE.md 而非 ~/.claude/CLAUDE.md` (value=3, effort=1, type=bugfix, score=0.845)

## 待办交接（下一轮从这里接续）

- 遗留 pending 候选: 3

| id | title | type | value/effort | ready |
|---|---|---|---|---|
| `candidate-426` | detect-agent 家目录探针查错文件：~/.CLAUDE.md 而非 ~/.claude/CLAUDE.md | bugfix | 3/1 | yes |
| `candidate-429` | smoke 建议不查 lint 脚本存在：npm run lint 记入后每轮红 | bugfix | 3/1 | yes |
| `candidate-433` | stop_server 无探活即 taskkill：PID 重用误杀无辜进程 | bugfix | 3/1 | yes |

- 按 score 降序（ready 优先）；新 run 用显式 --candidate-id 接续，无需重新挖矿
