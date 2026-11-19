# 实验与复现

[返回项目首页](../README.md) · [使用说明](USAGE.md)

本文区分三件事：**重算已有结果**使用保存的数据与模型输出；**生成新实验**会产生模型或行情用量；**真实交互验收**验证 CLI 和工具能否协同完成指定场景。所有命令在仓库根目录的 PowerShell 中执行。

## 完整离线复验

完成首页的环境安装后，无需填写模型 Key：

```powershell
.\.venv\Scripts\python.exe scripts/validate_v2.py
```

| 检查名 | 内容 |
| --- | --- |
| `unit_tests` | 单元测试与回归 |
| `dependency_check` | `pip check` |
| `dialogue_offline` | 保存数据与代码上的交互工具检查 |
| `offline_replay` | 独立进程执行固定案例保存的模型代码 |
| `resume_pipeline` | 对完整已有固定案例运行 `all --resume` |
| `generalization` | 多标的、多时段冻结代码回放 |
| `stability_replay` | 重跑保存的 15 份稳定性代码 |
| `factor_ideation_replay` | 当前校验器检查冻结表达式并回放 |
| `methodology` | 从已有产物汇总指标 |

控制台各项 `ok` 应全部为 `true`；逐项日志和 [validation_summary.json](../artifacts/quant_v2/validation_summary.json) 写入 `artifacts/quant_v2/`。最后一次完整 9 项检查对应 162 项测试，本机约 139 秒；随后增加 4 项审计器回归，当前 166 项测试单独通过，详见 [Memory 验收摘要](../artifacts/quant_v2/memory_online_validation.json)。这两次验证的范围与计时分开记录。

该流程以仓库已有实验完整为前提。`all --resume` 会检查已有生成；若用户删除了记录或指定未完成的目录，可能进入新生成并需要 Key。不能把它当作任何目录状态下都保证离线的命令。

## 固定 SMA 案例

配置：[quant_v2_config.json](../finrobot_autogen/experiments/quant_v2_config.json)。标的 `000001`，2021–2025 年，1,212 条真实日线，SMA 20/60，本金 100,000 元，开仓预算 80%。

```powershell
# 在独立进程中执行保存的 5 份代码并核对原结果
.\.venv\Scripts\python.exe -X utf8 finrobot_autogen\experiments\run_quant_experiment.py verify
# 完整已有记录上复用生成，重算传统对照与评估
.\.venv\Scripts\python.exe -X utf8 finrobot_autogen\experiments\run_quant_experiment.py all --resume
```

| 策略 | 总收益率 | 最大回撤 | Sharpe |
| --- | --- | --- | --- |
| 人工 SMA / 最终通过校验的 LLM SMA | −27.969558% | 35.485943% | −0.392180 |
| 同预算买入持有 | −24.237415% | 58.630890% | −0.100196 |

5 次独立生成中，4 次 JSON 首次合格，1 次 JSON 错误经一次模型修正通过；另有一次初始执行被过严的校验器拒绝，修正校验器后原代码通过。首次完整执行为 3/5，无需模型修正为 4/5，最终为 5/5。

证据：[evaluation.json](../artifacts/quant_v2/evaluation.json)、[comparison.csv](../artifacts/quant_v2/comparison.csv)、[audit.json](../artifacts/quant_v2/audit.json)、[comparison.png](../artifacts/quant_v2/comparison.png)。模型与人工实现同一规则，没有额外收益提升。

固定入口支持 `fetch`、`baseline`、`generate`、`repair`、`verify`、`evaluate`、`all` 和 `--resume`；`--refresh-data` 重新取数。生成目录为固定的 `generation_XX`，已有执行记录时不加 `--resume` 会拒绝覆盖；需要新生成时优先用聊天入口，或先规划独立批量结果位置。恢复已有记录不算新的独立生成。

## 多标的、多时段回放

配置：[generalization_config.json](../finrobot_autogen/experiments/generalization_config.json)。标的为 `000001`、`000651`、`600031`；评价时段为 2021–2022、2023–2024、2025，共 9 组。每组用此前 60 条观测预热，评价期重新初始化资金，策略和费用一致。

```powershell
# 已有快照上的冻结代码回放，不请求模型
.\.venv\Scripts\python.exe -X utf8 finrobot_autogen\experiments\run_generalization.py evaluate --resume
# 获取缺失行情并评价，可能访问行情服务
.\.venv\Scripts\python.exe -X utf8 finrobot_autogen\experiments\run_generalization.py all --resume
# 自定义配置文件，按参数隔离结果
.\.venv\Scripts\python.exe -X utf8 finrobot_autogen\experiments\run_generalization.py all --config path\to\my_config.json --resume
```

冻结固定案例的 5 份模型代码，共回放 45 次，全部与人工同规则结果一致；这是 45 次执行，不是 45 次模型生成。SMA 相对买入持有有胜有负，全部结果保留，不证明任意市场有效。

结果入口：[latest.json](../artifacts/quant_generalization/latest.json)。运行目录包含 `inputs.json`、`summary.json`、`comparison.csv/png` 和逐案例指标、净值、交易与代码检查。

## 因子构思与样本外评价

配置：[factor_ideation_config.json](../finrobot_autogen/experiments/factor_ideation_config.json)。模型仅看 `000001` 的 2021–2023 数据摘要，每次提出 4 个受限表达式因子；表达式冻结后回放到三个标的的 2024–2025，与 SMA 20/60 及买入持有比较。

```powershell
# 复现已有因子结果，无需 Key
.\.venv\Scripts\python.exe -X utf8 finrobot_autogen\experiments\run_factor_ideation.py evaluate
# 生成新实验：需 Key，计划 3 次模型请求，使用新目录
.\.venv\Scripts\python.exe -X utf8 finrobot_autogen\experiments\run_factor_ideation.py generate --out-root artifacts/factor_ideation_new
.\.venv\Scripts\python.exe -X utf8 finrobot_autogen\experiments\run_factor_ideation.py evaluate --out-root artifacts/factor_ideation_new
```

已有 3 次生成返回 12 个有效想法，去重后 9 个，跨三个标的共 27 个样本外比较：**0/27 优于买入持有，12/27 优于 SMA（44.44%）**。正式评价窗口晚于构思窗口，不根据样本外成绩重新选择规则或调参。

冻结表达式使用当前嵌套预热规则重算，EMA 用完整历史前缀；构思期常量信号、历史不足或未定义信号拒绝评价。原始生成与数据保留。技术限制见[使用说明](USAGE.md#独立表达式因子实验)。

结果入口：[latest.json](../artifacts/factor_ideation/latest.json)。`ideation_XX/ideas.json` 保存逻辑、表达式、参数与风险，`idea_revalidation.json` 记录当前校验，`summary.json` 区分计划、完成、失败与缺失，`comparison.csv/png` 保存对照。

## 多案例生成稳定性

配置：[stability_config.json](../finrobot_autogen/experiments/stability_config.json)。3 个标的/区间/参数组合，每组重复生成 5 次，共 15 次独立模型请求。

```powershell
# 汇总已有结果，无需 Key
.\.venv\Scripts\python.exe -X utf8 finrobot_autogen\experiments\run_stability.py evaluate
# 独立进程重新执行保存的 15 份代码
.\.venv\Scripts\python.exe -X utf8 finrobot_autogen\experiments\run_stability.py verify
# 新实验：需 Key，计划 15 次模型请求，使用新目录
.\.venv\Scripts\python.exe -X utf8 finrobot_autogen\experiments\run_stability.py generate --out-root artifacts/stability_new
.\.venv\Scripts\python.exe -X utf8 finrobot_autogen\experiments\run_stability.py evaluate --out-root artifacts/stability_new
```

15/15 次生成通过，JSON、规则、前缀因果性与可信引擎一致性均为 100%；生成延迟均值 7.90 秒、中位 7.53 秒，SDK token 合计 27,001。重跑代码不计为新的独立生成，结果不代表投资收益。

结果入口：[latest.json](../artifacts/stability/latest.json)。`attempts.csv` 保存逐次尝试，`summary.json` 保存总体与分案例统计；分母包含失败和缺失，条件通过率单独列出。离线 `verify` 写 `verification.json` / `verification_summary.json`，不覆盖首次 `execution.json`。

以上 `_new` 仅是目录名示例，每组新实验使用新目录。恢复未完成的生成可用 `--resume`，补齐缺失尝试仍可能产生模型用量。提示词变化时不能静默混用旧结果，按脚本提示创建新目录。

## 结果与验收口径

```powershell
.\.venv\Scripts\python.exe scripts/evaluate_agent_methodology.py
```

该命令只读取已有产物，不调用模型或重算回测，生成 [methodology_metrics.md](../artifacts/quant_v2/methodology_metrics.md) 与 [methodology_metrics.json](../artifacts/quant_v2/methodology_metrics.json)。

| 指标 | 含义与范围 |
| --- | --- |
| 首次 JSON 合格率 | 固定案例首次响应满足结构要求的比例，4/5 = 80% |
| 首次完整执行成功率 | 首次生成在当时流程完整通过的比例，3/5 = 60% |
| 无需模型修正成功率 | 未重新请求模型即通过的比例，4/5 = 80% |
| 最终成功率 | 包含修正后最终通过的比例，5/5 = 100% |
| 规则、因果性、引擎一致性 | 实现、输入前缀与可信计算的一致性；不等于预测能力 |
| 数值引用忠实度 | 最终回答引用的数字与实际工具结果对照，仅按已核对字段计数 |
| 多轮行为 | 角色、参数、实验 ID、规则保留与切换；按具体真实场景验证 |
| 稳定性及样本外结果 | 独立生成与冻结回放分别计数，失败、缺失及负面收益全部保留 |

所有在线验收只说明这些场景通过，不是多次独立运行的成功率估计，也未覆盖所有角色、股票或日期。

Memory 的第 9 个真实回答返回完整规则本身，未按要求外包 `rule_json`。后端本来接受这两种形状；审计器据此兼容，但继续逐字段核对策略名、条件、阈值与执行方式。原始回答未改写，输出格式偏差和最初 KeyError 保留，离线重审没有追加模型请求。共 9 个场景、13 个模型响应、34,596 token。另一次本地 PowerShell 临时检查把中文追问编码成问号，改用 UTF-8 源文件核对后角色恢复正常，产品代码未修改。

## 真实交互验收

以下命令均调用真实模型，需要本地 Key 并产生用量；行情工具可能联网。不要把它们与上面的离线复现混为一谈。

```powershell
# 三轮：参数化 SMA、同实验代码生成、换股与风险
.\.venv\Scripts\python.exe scripts/validate_quant_dialogue.py --online
# 五轮自动模式，明确关闭 Memory 以保持该验收场景
.\.venv\Scripts\python.exe scripts/validate_auto_research.py --online
# 只验证自动模式中的多角色汇总场景
.\.venv\Scripts\python.exe scripts/validate_auto_research.py --online --only multiple_roles
# 九个 Memory 场景：每轮新 CLI 进程，使用独立验收数据库
.\.venv\Scripts\python.exe scripts/validate_memory_online.py --online
```

| 已保存验收 | 核对范围 | 脱敏证据 |
| --- | --- | --- |
| 量化三轮 | 用户参数、实验复用、代码执行、切换标的、风险与数字 | [dialogue_online_validation.json](../artifacts/quant_v2/dialogue_online_validation.json)、[console_output_validation.json](../artifacts/quant_v2/console_output_validation.json) |
| 自动模式五轮 | 因子计算、规则提出、续聊回测、换股、多角色汇总，36 项数值检查 | [auto_online_validation.json](../artifacts/quant_v2/auto_online_validation.json) |
| Memory 九场景 | 长回答规则保存、自动/单角色重启恢复、新会话默认隔离、显式跨会话检索、笔记增删、16 项数值检查 | [memory_online_validation.json](../artifacts/quant_v2/memory_online_validation.json) |

Memory 还核对 `--no-memory` 不创建数据库、角色恢复以及删除笔记后旧回答仍保留。各场景使用唯一策略名和非默认三条件规则，避免靠示例参数蒙对；它是用户指定规则的精确恢复测试，不是自由因子发现。

原始对话、数据库、控制台和实际使用的行情快照存于 Git 忽略的 `artifacts/agent_chats/` 等本地目录，不随仓库分发。观察器只记录工具输入输出与 SDK 用量，不替换响应。有原始记录时可离线重审，将路径替换为本机实际 `run`：

```powershell
.\.venv\Scripts\python.exe scripts/validate_auto_research.py --audit artifacts/agent_chats/auto_validation/20261119T104307855667
.\.venv\Scripts\python.exe scripts/validate_memory_online.py --audit artifacts/agent_chats/memory_validation/20261119T134935141193
```

新模型请求不保证逐字复现旧回答，SDK token 不等同于准确费用，实际账单以服务商为准。基本面、新闻、PDF/OCR/RAG 与保留的多 Agent 示例不在完整在线验收范围内。
