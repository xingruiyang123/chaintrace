# 链迹追踪 · 算法一规则引擎原型 V0.1

> **性质：离线验证程序 / 全部使用虚构模拟数据。** 不包含真实人员、钱包地址或实际侦查数据；不能输出人员身份或实时位置结论，不具备直接投入公安生产环境的条件。

本程序是依据已提供的《算法一-链上链下多源交易关联匹配方法设计.md》以及数据库 `schema_v1_1_1.sql` 开发的**可执行、可自动测试的最小原型**。它将链上转账事件、结构化供述/报告主张和来源材料进行多维候选关联，输出与 `candidate_match` 概念对应的结构化结果。

## 本次已经实现的功能

- **同案分组与候选筛选**：按案件、资产、时间索引搜索，若外部交易标识相同也保留跨资产冲突诊断。
- **资金事件—主张匹配**：严格比较资产网络、外部编号、金额、手续费、时间和账户方向。
- **冲突与缺失分离**：`conflict_items` 与 `missing_items` 互不混淆，缺字段不自动解释为相反证据。
- **链上事件与平台入账分离**：仅在交易哈希、充值地址、资产、金额、时间均一致时建立 `EVENT_TO_EVENT` 候选核对，**不合并交易**。
- **证据来源完整性检查**：测试数据原始负载以固定的 JSON 规范化序列计算 SHA-256，修改负载会触发冲突。
- **谨慎的人—账户关系处理**：只有带证据、经过复核且未争议的模拟映射才可用于账户方向匹配；姓名/字符串相同不能视为钱包归属。
- **案件级算法运行记录**：每个案件单独产生 `analysis_run` UUID、输入快照摘要、算法版本和参数；结果为 `PENDING` 或 `DISPUTED`，绝不自动变为 `ACCEPTED`。
- **负向测试与评价**：重复导入、错误时间、资产网络冲突、方向相反、哈希篡改、跨案数据、未核实身份等。

## 目录

```
chaintrace_algorithm1_v0_1/
├─ chaintrace_matcher.py       算法引擎（Python 标准库）
├─ generate_fixture.py        固定测试数据生成器
├─ benchmark_synthetic.py     困难负样本压力实验
├─ run_experiments.py         离线执行入口
├─ fixtures/
│  └─ synthetic_cases.json    A01–A12 虚构测试场景
├─ tests/
│  └─ test_matcher.py         pytest 自动测试
├─ outputs/
│  ├─ analysis_runs.json
│  ├─ candidate_matches.json
│  ├─ metrics.json
│  └─ benchmark_metrics.json
└─ 实验结果与数据库衔接说明.md
```

## 运行方法（Python 3.10+）

在本目录下执行：

```bash
python generate_fixture.py
python run_experiments.py
python benchmark_synthetic.py --events 400 --claims 200 --seed 20261104
python -m pytest -q
```

引擎本身只需要 Python 标准库；运行自动测试需要 `pytest`。Windows 下可使用 `py -3` 替代 `python`。本项目没有调用公链 API、真实交易所、人员数据库或网络服务。

## 数据结构约定

- `events`：标准化资金事件，包含 `case_id`、`event_type`、`asset_id`、`network_id`、`amount`、`event_time`、双方账户、可选外部交易标识和来源引用。
- `claims`：证据中抽取的结构化付款主张，包含 `predicate`（仅 `PAID_TO`、`RECEIVED_FROM`、`TRANSFERRED_TO`）、主客体类型与标识、资产、金额、时间范围、可选外部标识及期望事件类型。
- `records`：原始来源负载、SHA-256、模拟标志以及案件归属。
- `verified_person_accounts`：经过人工核验的人员—账户链接；模拟实验专用，不可将任何模型推断标记成“已核验”。

所有测试 `case_id` 均为确定性 UUID。输出 `left_node_id` 和 `right_node_id` 也是根据业务 ID 合成的演示 UUID，**它们并不是现有数据库里的真实 `entity_node.node_id`**，不能直接把输出 JSON 当成 PostgreSQL `INSERT` 脚本。

## 评分及其边界

基础权重沿用设计稿：外部标识 0.35、资产网络 0.20、金额 0.20、时间 0.15、方向账户 0.10。针对缺失字段，评分会计算已观测维度的加权平均，并另外记录覆盖率，防止单一字段碰巧相同就作出高等级推断。遇到硬冲突、信息不足、未核实的人—账户关系时，输出的排序分设为 `null`，诊断细节仍在 `explanation` 中。

`EXACT` 只指**具体交易信息的精确匹配**，不是“身份已认定”；`CANDIDATE_STRONG` 不是具有统计学意义的置信度；所有等级仍须人工复核。设计稿中的分数阈值与时间窗口是未经业务标定的实验值，绝不可直接当成实战标准。

## 与 V1.1.1 数据库衔接尚缺的适配工作

1. `claim` 表没有独立保存 **交易哈希/平台订单号/期望资金事件类型**。本离线 DTO 有 `external_id`、`external_id_type` 和 `expected_event_type` 字段，未来需要明确从哪份 `evidence_item`、`source_record` 或结构化证据表获得，不能凭空推断。可考虑新增带证据来源的 `claim_reference` 表或扩充受控证据主张结构。
2. 将真实的 `entity_node.node_id` 替换演示 UUID；严格执行 `(case_id, node_id)` 复合外键。
3. 应从 PostgreSQL 已获授权的只读视图取数，配置实际 `case_access` + RLS，处理真实来源授权、审计和脱敏。**当前 SQL 尚未启用 RLS。**
4. **跨平台 JSON 哈希一致性**：引擎使用 `json.dumps(sort_keys=True, separators=(',', ':'), ensure_ascii=False)` 产生字节序列；冒烟 SQL 使用 `digest(raw_payload::text,'sha256')`。两者对 JSON 键序及空格的序列化不保证一致。上线前应统一原始字节哈希或者采用明确版本的规范化标准，不能直接对照两个哈希字段。
5. 本原型对于同一链上哈希与收发地址的重复记录实行保守拒绝；真实交易可能包含**同一交易中的多个转账日志**，须接入数据库里的 `tx_index` / `tx_index_kind` 才能正确去重。
6. 大批量运行还需引入金额阻塞过滤、来源适配器、事务化写回、审计及异常重试。目前可作为算法验证，不能直接接入生产侦查数据。
7. 本原型 **不包含 PostgreSQL 建库执行验证**。上传的 `schema_v1_1_1.sql` 与 `db_smoke_test_v1_1_1.sql` 仍需在实际 PostgreSQL 15+ 环境顺序执行。

## 相关参考（用户提供）

- `schema_v1_1_1.sql`（数据库 30 表版本）
- `db_smoke_test_v1_1_1.sql`（数据库冒烟测试，尚待执行）
- 《算法一-链上链下多源交易关联匹配方法设计.md》
- 《V1.1.1数据库修订说明.md》

> 本原型不会分析真实人员的位置，也不会根据交易活动自动指认犯罪嫌疑人。验证结果只代表人工构造的离线测试情况。
