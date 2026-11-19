# 使用说明

[返回项目首页](../README.md) · [实验与复现](EXPERIMENTS.md)

本文补充角色、规则、Memory 与环境细节。首次安装和最短启动步骤见[项目首页](../README.md#安装与运行)。以下命令均在仓库根目录的 PowerShell 中执行。

## 角色与启动方式

统一入口为 `run_finrobot.cmd`。不带问题进入多轮对话，带问题执行单次分析；默认自动分派，单独指定 `--role` 等同固定单角色。`--mode auto` 不能与 `--role` 同用。

| 角色 | 负责内容 | 验证范围 |
| --- | --- | --- |
| `A_Share_Analyst` | 公司资料、财务、估值、同业与相关投研数据 | 已验证初始化和基础模型工作流，完整基本面流程未逐项验收 |
| `Market_Analyst` | 行情表现、市场事件、新闻、公告与情绪 | 已验证初始化，全部接口与完整分析流程未逐项验收 |
| `Expert_Investor` | 年报、财务、机构数据、图表及 PDF 研究报告工具 | 代码保留并有修复，完整 PDF/OCR 流程未验收 |
| `Quant_Analyst` | 参数化 SMA、买入持有、固定规则代码生成与风险查询 | 真实多轮交互与数值核对通过 |
| `A_Share_Factor_Researcher` | 确定性价量因子、固定动量回测与基准比较 | 因子计算和分项汇总通过真实验收；固定动量工具有离线回归 |
| `A_Share_Factor_Ideator` | 因子/规则构思、受限 JSON 规则回测 | 真实规则构思、续聊、换股及数值/账本核对通过 |

```powershell
# 自动分派：多轮或单次
.\run_finrobot.cmd
.\run_finrobot.cmd "回测000651在2023-2024年的10/30均线并检查风险"
# 固定角色
.\run_finrobot.cmd --role Quant_Analyst
.\run_finrobot.cmd "分析600519的盈利和估值" --role A_Share_Analyst
.\run_finrobot.cmd --role Market_Analyst
.\run_finrobot.cmd --role Expert_Investor
# 模型、数据源与调试输出
.\run_finrobot.cmd --role Quant_Analyst --model deepseek-flash --provider akshare --verbose
# 查看参数，不调用模型
.\run_finrobot.cmd --help
```

自动模式用规则匹配问题意图，支持股价/新闻、基本面、参数化 SMA、因子和自定义规则等。单角色直接回答；多角色分别完成本轮分项，再由综合角色汇总。失败项会列出，各角色的工具按任务范围限制，不应混用股票、日期或指标。

默认终端只显示等待提示、当前最终分析和保存位置，调试过程写入日志。`--verbose` 显示工具与调试细节。输入 `exit`、`quit`、`:q` 或 `/exit` 退出，更新代码后退出旧进程再启动。

## 模型与数据配置

配置模板为 [OAI_CONFIG_LIST.example.json](../finrobot_autogen/OAI_CONFIG_LIST.example.json) 和 [config_api_keys.example.json](../finrobot_autogen/config_api_keys.example.json)，复制方法见首页。实际文件分别为不带扩展名的 `OAI_CONFIG_LIST`、`config_api_keys`，不能把 Key 写入模板或文档。

默认模型名为 `deepseek-flash`，地址为 `https://api.deepseek.com/v1`，保留模板中的 `extra_body.thinking.type=disabled`。切换模型时，配置的名称须与 `--model` 完全一致，服务须支持本项目使用的 OpenAI 兼容工具调用；其他模型未逐一验证。原框架配置说明见 [CONFIG_README.md](../finrobot_autogen/CONFIG_README.md)，当前文件名和默认值以项目首页为准。

`CN_DATA_PROVIDER` 或 `--provider` 支持：

| 值 | 行为 |
| --- | --- |
| `akshare` | 模板默认；无需 Tushare Token |
| `auto` | 对支持的接口优先尝试 Tushare，失败回退 AkShare；新闻、公告及部分财务接口仍走 AkShare |
| `tushare` | 只调用 Tushare 支持的接口并暴露错误，需有效 Token 和相应权限 |

量化数据统一 OHLCV、日期、排序和成交量单位，保存来源、复权、处理记录及哈希，不补造缺失交易日。交互快照仅在标的、复权、日期覆盖、预热与哈希均符合要求时复用。

历史 BVPS 没有过去报告期时返回无可用值；报告财务表按报告期与披露日期过滤，不能当作历史修订版本的完整重建。年报选择按报告年份匹配正式年报，排除摘要、英文、更正与取消公告，无有效结果时明确返回未找到。

## 量化工具与策略范围

`Quant_Analyst` 使用以下工具，结果数字来自工具与保存文件：

| 工具 | 作用 |
| --- | --- |
| `prepare_quant_experiment` | 按用户股票、日期、均线、资金与预算准备实验，返回独立 `experiment_id` |
| `compare_quant_strategies` | 对交互实验运行 SMA 与买入持有，保存指标、净值、交易和图表 |
| `execute_quant_code` | 保存、校验并限时执行当轮模型代码，返回执行结果或错误 |
| `check_quant_risk` | 根据成交记录独立重建现金与持股，核对净值、指标、整手、资金、预算与时序 |
| `get_data_summary` | 读取固定案例摘要与数据哈希 |
| `backtest_reference` | 对固定案例运行人工 SMA 或买入持有 |
| `read_backtest_result` | 读取固定案例已有指标与交易 |

代码接口为 `generate_signals(data, fast, slow)` 与 `run_strategy(data, config)`。生成的是固定 SMA 的实现，须通过语法白名单、接口、信号规则、前缀因果性和可信引擎一致性检查。独立进程限时执行，原始响应、代码、错误与修正保留；不支持任意 Python 策略。

SMA 规则为收盘均线 `SMA(fast) > SMA(slow)` 时目标持有，否则空仓。信号最早下一有行情交易日开盘执行。交互可改股票、区间、均线、资金与开仓预算；其他费用假设由实验配置控制。`max_position` 是每次开仓预算，持仓后权重可随价格漂移。

三个量化角色禁用通用 Python 执行，只调用注册工具。白名单和进程超时是任务约束，不是通用操作系统沙箱。

## 因子与规则 DSL

### 确定性价量因子

| 因子 | 公式 | 单位 |
| --- | --- | --- |
| 20/60 日动量 | `Close / Close.shift(N) - 1` | 小数 |
| 20 日波动率 | 日收益标准差 × √252 | 年化小数 |
| 20 日均量 | 最近 20 根日线成交量均值 | 手 |

```text
用 get_a_share_factors 看一下600519截至2025-06-30的动量和波动率。
帮我用20日动量大于0且5日均量高于20日均量的规则回测000001，2021-06-01到2022-12-31。
```

### 聊天中的 JSON 规则

`A_Share_Factor_Ideator` 提出规则数据，校验后调用确定性回测，不执行模型提供的任意 Python。支持 `momentum`、`average_volume_ratio`、`moving_average_cross`（收盘价或成交量），比较符为 `>`、`>=`、`<`、`<=`、`==`，逻辑组为 `all` / `any`。

```json
{
  "name": "volume_confirmed_momentum",
  "signal": {"all": [
    {"indicator": "momentum", "window": 20, "operator": ">", "value": 0},
    {"indicator": "average_volume_ratio", "short_window": 5, "long_window": 20, "operator": ">", "value": 1}
  ]},
  "execution": "next_open"
}
```

窗口为 1–252，嵌套组总计最多 12 个条件、深度不超过 8，JSON 字符串最多 16,000 字符，执行方式仅支持 `next_open`。预热不足时拒绝评价；成本与账本审计沿用量化引擎，同预算买入持有作为对照，沪深 300 仅在取数成功时提供。

这些因子/规则工具的常规调用在线取数，不自动写入固定批量实验的快照证据链。验收脚本另保存工具实际使用的快照用于审计；需要固定、可重复的数据证据时，使用 `Quant_Analyst` 的实验快照或批量实验入口。

### 独立表达式因子实验

表达式 DSL 使用 `sma/ema/std/ret/shift/zscore/rank/delta` 等尾部原语，与聊天 JSON 规则是两个入口。它按构思期摘要生成表达式，冻结后做样本外回放，命令见[实验说明](EXPERIMENTS.md)。

表达式不允许属性、下标、导入、字符串、任意 Python 或乘幂；上限为 4,000 字符、256 个 AST 节点、32 层，数值字面量有限且绝对值不超过 `1e12`。拒绝乘零退化和构思期常量信号。嵌套预热按表达式计算，EMA 使用冻结快照的完整历史前缀，预热不计交易和绩效。

## 本地研究记忆

默认每次启动新建持久会话，数据库为 `data/memory/research.sqlite3`。退出后按终端给出的会话 ID 恢复；必须沿用原模式、角色和数据库。

```powershell
# 自动模式恢复：替换为实际会话 ID
.\run_finrobot.cmd --session <会话ID>
# 固定角色恢复时保留原角色
.\run_finrobot.cmd --role Quant_Analyst --session <会话ID>
# 使用另一份本地数据库，相对路径按仓库根目录解析
.\run_finrobot.cmd --memory-db .\data\memory\project.sqlite3
# 本次完全不访问 Memory 数据库，不能与 --session 同用
.\run_finrobot.cmd --role Quant_Analyst --no-memory
```

| 本地命令 | 作用 |
| --- | --- |
| `/sessions` | 列出已有会话的 ID、模式与角色 |
| `/memory` | 查看当前会话、可用笔记与命令帮助 |
| `/remember TEXT` | 显式保存笔记，返回笔记 ID |
| `/recall QUERY` | 按股票或关键词查找笔记和完成的研究，可跨会话 |
| `/forget ID` | 删除对应显式笔记 |

这些命令在读取模型配置前处理，可以无 Key 单次执行，例如 `.\run_finrobot.cmd "/sessions"`。`/forget` 的 ID 使用实际返回值。

Memory 保存问答原文、角色、状态、实验 ID、来源、日期和部分工具证据；只有完成的研究用于自动恢复与检索。模型默认获得本会话最近三轮的有界摘要，新会话不自动注入其他会话问答；显式笔记在同一数据库共享，`recall_research` 可按请求跨会话查询。这是同一使用者的本地研究组织方式，不是多用户权限隔离。

通过 DSL 校验的完整 JSON 规则单独保存 `rules`、哈希和来源，不表示已经回测或盈利。上下文优先放入完整规则，放不下则整条省略并标记 `rules_omitted` / `truncated`，不会截成半条；可用 `recall_research(limit=1)` 重查。旧库兼容新增规则列，原文中存在完整规则时可补提取，不能凭空恢复原本不存在的规则。

`/forget` 删除笔记并刷新后续上下文，**不删除旧问答、日志或实验文件**。`--no-memory` 只关闭数据库访问，原有分析文件仍保存。运行中的记忆读写失败会提示并保留已生成分析；启动时数据库打不开会报错，可使用 `--no-memory` 继续。旧结论必须按原来源和日期核验，不能代替实时事实。

Memory 不增加额外 LLM/embedding 请求，不训练模型或自动进化策略；正常提问仍调用模型。真实续聊验收范围及输出包装差异见[验收口径](EXPERIMENTS.md#结果与验收口径)。

## 环境与常见问题

完整版本见 [requirements-windows-py311.lock.txt](../requirements-windows-py311.lock.txt)，兼容约束见 [constraints-windows-py311.txt](../constraints-windows-py311.txt)。主要组件为 pyautogen 0.2.35、openai 1.55.3、httpx 0.27.2、numpy 1.26.4、pandas 2.0.3、backtrader 1.9.78.123、akshare 1.20.7；torch 2.5.1 CPU 与 onnxruntime 1.19.2 用于保留的 PDF/RAG 依赖兼容。

已有 uv 时可用以下命令替代首页的环境创建与两条 pip 安装，随后仍须下载 NLTK 数据、执行依赖检查：

```powershell
uv venv --python 3.11 --cache-dir .cache/uv .venv
uv pip install --python .venv/Scripts/python.exe --cache-dir .cache/uv -r requirements-windows-py311.lock.txt
uv pip install --python .venv/Scripts/python.exe --cache-dir .cache/uv --no-deps --no-build-isolation -e .
```

| 现象 | 检查方式 |
| --- | --- |
| 找不到启动脚本、依赖或数据 | 回到同时包含 `setup.py` 与 `run_finrobot.cmd` 的仓库根目录 |
| `py -3.11 --version` 失败 | 先安装 Python 3.11；不要直接改用 Python 3.13 |
| 配置文件找不到或模型配置为空 | 实际文件没有扩展名；JSON 不允许注释，模型名称与 `--model` 一致 |
| 模型请求失败或没有最终分析 | 查看本轮保存的 `.log` 或加 `--verbose`，按实际错误检查 Key、网络与工具 |
| 角色/模式与会话不匹配 | `/sessions` 查看原模式、角色，恢复时沿用相同数据库 |
| 图片未在终端内显示 | 图像工具返回绝对路径，用本机图片查看器打开；Notebook 中可显示图片 |

启动警告写入 `.cache/agent_startup.log`。PDF/OCR/RAG 并未完整验收：OCR/embedding 权重未预下载，环境初查未找到 `wkhtmltopdf`，安装 Python 包不等于跑通报告生成。其他操作系统和 Python 3.10 未做同范围的最终复验。

## 实现参考

本地 Memory 按本项目 AutoGen CLI 独立实现，设计参考官方 FinRobot 固定提交 [`63e1884`](https://github.com/AI4Finance-Foundation/FinRobot/tree/63e1884a42ea7b729551a5424b4bd6a65af636e0) 的本地持久化方式：

- [audit/transcript.py](https://github.com/AI4Finance-Foundation/FinRobot/blob/63e1884a42ea7b729551a5424b4bd6a65af636e0/finrobot_desktop/finrobot/audit/transcript.py) 与 [audit/persistence.py](https://github.com/AI4Finance-Foundation/FinRobot/blob/63e1884a42ea7b729551a5424b4bd6a65af636e0/finrobot_desktop/finrobot/audit/persistence.py)。
- [artifact/sqlite_store.py](https://github.com/AI4Finance-Foundation/FinRobot/blob/63e1884a42ea7b729551a5424b4bd6a65af636e0/finrobot_desktop/finrobot/artifact/sqlite_store.py) 与 [coverage/sqlite_store.py](https://github.com/AI4Finance-Foundation/FinRobot/blob/63e1884a42ea7b729551a5424b4bd6a65af636e0/finrobot_desktop/finrobot/coverage/sqlite_store.py)。
