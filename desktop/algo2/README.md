# algo2 · 混币—法币出口识别算法

纯标准库（Python 3.13），不装任何第三方依赖。输入一份「地址—交易」有向图，输出四组结果：
**混币池识别**、**入金↔出金断链归因**、**法币出口识别与可控性**、**追缴路径方案**。

> 全部数据为合成数据。这里的分数是"证据够不够"，不是"案件成立不成立"；
> 混币池与断链归因都是候选，不表示资金转移已确认。

---

## 1. 算法思路

| 步骤 | 模块 | 做法 | 关键取舍 |
|---|---|---|---|
| 读入 | `graph.py` | 校验节点/边引用、金额（十进制字符串）、时间（UTC 带 Z）、`is_simulated is true` | 金额一律 `Decimal`，拒绝 JSON 数字——`0.1+0.2` 的坑不在算法里补，在门口挡掉 |
| 池识别 | `mixer_detect.py` | 七个信号 M1–M7，`detection_score = min(100, Σ 命中权重)` | 命中与**未命中**都输出 value/threshold/detail，界面能回答"为什么不是它" |
| 归因 | `attribution.py` | 同一池内入金×出金全配对，六维各归一 0–100，`score = Σ(value×weight)/100` | 入出金数量不等时允许一对多；每条都给 `rank` 与 `tier`，**同分同名次** |
| 出口 | `offramp.py` | 规则库 `rules/offramp_rules.json`，`S_off = min(cap, Σ 规则分)` | 每条命中都带 `rule_id` 与中文说明，前端不内置任何分数 |
| 可控性 | `controllability.py` | `S_profile` 五维加权 + 优先通道加分；`S_ctrl = clamp(0,100, .65·S_profile + .25·S_off + .10·C − P)` | 公式的每个输入值都写进结果（含 `formula` 字符串），界面可原样复算 |
| 路径 | `paths.py` | 主路径 + 备选，`score = .5·归因分 + .5·出口可控性 − 跳数惩罚` | 只走本案的钱：起点是涉案源、每条边从起点可达、不穿混币池、不用噪声边；备选必须换拦截点，凑不出就只给一条 |
| 串联 | `pipeline.py` | 输出契约 §3 全结构，并跑一遍 `validate_result()` 自检 | 自检不过直接抛错，不落盘半成品 |

候选池的两个来源：节点标了 `type=mixer`，或结构上像池（入金来源数 ≥ `min_inputs`、出金 ≥ 2、混币类边占比过半）。
节点标签只是线索，分数完全由结构证据算出来。

## 2. 参数表

顶层 `parameters` 只放契约 §3 已列的键；信号自身的阈值与权重随信号一起输出（`signals[].threshold` / `signals[].weight`）。

| 组 | 键 | 取值 | 作用 |
|---|---|---|---|
| mixer | `min_inputs` | 3 | M1 判定"多输入聚合"的最低入金地址数 |
| mixer | `equal_output_tolerance` | 0.0005（0.05%） | 等额切片、找零判定的相对容差 |
| mixer | `batch_window_hours` | 48 | 批处理/轮次切分的窗口 |
| mixer | `min_equal_outputs` | 3 | M2 判定"等额输出"的最低同额笔数 |
| attribution | `weights` | amount 30 / time 20 / denomination 15 / path_rarity 15 / cooccurrence 12 / fee_fit 8 | 合计 100，代码里断言 |
| attribution | `tier` | high 70 / medium 45 | HIGH ≥70，MEDIUM ≥45，其余 LOW（边界归上档） |
| offramp | `cap` | 100 | `S_off` 的上限 |
| controllability | `alpha/beta/gamma` | 0.65 / 0.25 / 0.10 | `S_ctrl` 三项权重 |
| controllability | `penalty` | high 15 / medium 8 / low 2 | 按出口节点风险等级扣分 |
| controllability | `signal` | green 75 / yellow 60 | GREEN ≥75，YELLOW ≥60，其余 RED |

信号权重（`mixer_detect.SIGNAL_WEIGHTS`，合计 126，靠 min(100) 截断）：

| 信号 | 含义 | 权重 | 判定 |
|---|---|---|---|
| M1 | 多输入聚合 | 20 | 不同入金地址数 ≥ min_inputs |
| M2 | 等额输出 | 22 | 最大等额组 ≥ min_equal_outputs 笔 |
| M3 | 批处理时间窗 | 16 | 出金跨度 ≤ batch_window_hours 且 ≥2 笔 |
| M4 | 地址一次性 | 14 | 只用过一次的收款地址 ≥2 个 |
| M5 | 面额标准化 | 12 | 有效数字 ≤3 位的出金占比 ≥60% |
| M6 | 入出金不守恒 | 16 | 差额占入金 0.01%–3%（服务抽成区间） |
| M7 | 固定面额存提 | 26 | ≥2 笔同面额存款且有同面额提现 |

六维归因的取值口径：

- **amount**：抽成区间内 100→85 线性；超出后按指数衰减（8% 的缺口已经不是"同一笔钱"）。
- **time**：≤72h 得 85–100；72–240h 得 40–85；>240h 再衰减；出金早于入金直接低分。
- **denomination**：与入金同量级且在等额组内 100；等额但金额不匹配 70；明显找零 8；其余 30。
- **path_rarity**：出金地址入度 + 2 跳内是否继续流向法币出口（不穿过其它混币池）。
- **cooccurrence**：入金是否落在该轮出金之前 240h 内；同轮笔数越多，单对确定性越低。
- **fee_fit**：隐含抽成与 0.01%–3% 的贴合度，最优 0.5%。

### 名次规则（同分同名次）

同一笔入金下，名次用**标准竞赛排名**：`rank = 1 + 分数严格高于本行的候选数`。
三笔出金如果六维证据完全相同、得分都是 91.06，就都记 `rank: 1`，下一档直接跳到 4（1,1,1,4），
不会硬排成 1/2/3。理由是同额批次里这类并列意味着**证据上无法区分**，硬排名次会让人误读成
"算法排错了"。界面上看到三个 `#1`，正确解读是"这三笔需要下游证据再分"。

`summary.distinct_rank1` 统计"真正只有一个第一名"的入金笔数——它才代表可唯一定位的数量，
而 `summary.high_links` 只说明有多少条候选达到 HIGH 分，两者不能互相替代。

### 路径规则（只走本案的钱）

1. **起点**只能是 tags 含 `SIM_SOURCE` 的涉案源；没有该标签时退化为"风险 high 的钱包里入度最小的一个"。
2. **可达性**：先对本案图做一次从起点出发的 BFS 得到可达边集合，路径只允许走这些边；
   上游段（入金之前的来路）**不穿过混币池**——穿过就说明那不是本案资金的来路。
3. **噪声边**：`ground_truth.noise_edges` 标注的外部用户边一律排除（真实数据没有该字段时自动失效）。
4. **备选路径**必须换拦截点才有资格出现，并在 `differentiator` 里写明差异；凑不出第二条就只给一条，
   `fallback_path_id: null`。出口本身是红灯时，拦截点前移到出金落地地址，`differentiator` 会说明原因。

## 3. 出口规则库

`rules/offramp_rules.json` 分 `node_rules` 与 `edge_rules`，每条：
`{rule_id, label, when, score, explain_template}`。`when` 内条件为"与"关系，支持：

- 节点规则：`type` / `risk` / `has_entity` / `text_any` / `min_deposit_count` / `min_inflow`
- 边规则：`pattern`+`min_count` / `min_distinct_sources` / `min_deposit_count` / `min_integral_deposits` / `mixer_within_hops` / `no_out_edges`

模板占位符（`{inflow_total}` `{deposit_count}` `{mixer_label}` 等）由上下文填充，不认识的占位符原样保留、不抛错。
**边界**：出口候选只取已标注为服务节点的地址；普通钱包即便有兑现特征也不判为出口——宁可少报，不把中转地址当兑付主体。

## 4. 怎么跑

```powershell
cd desktop
D:\anaconda\python.exe -m pytest algo2\tests -q              # 32 项
D:\anaconda\python.exe -m algo2.generate_fixture             # 生成评估集
D:\anaconda\python.exe -m algo2.benchmark                    # 跑基准，落盘 outputs/benchmark.json
D:\anaconda\python.exe 离线原型\mixer_service.py 离线原型\样例\混币出口虚拟样例.json <空目录>
```

桥接侧（Electron）：

```powershell
echo '{"action":"mixer-run","path":"<样例绝对路径>"}' | D:\anaconda\python.exe -X utf8 离线原型\desktop_bridge.py
```

`mixer-run` 返回 `{ok, result, dataset}`，`mixer-export` 落盘
`混币出口识别结果.json` + `断链归因.csv` + `运行记录.json`（含 `input_sha256` 与 `is_simulated:true`）。

## 5. 指标口径

评估集 `fixtures/mixer_eval_cases.json`：**8 个虚拟案件、254 条边、31 对构造真值**，程序生成。

| 指标 | 定义 |
|---|---|
| p@1 | 给定一笔入金，正确出金是否排第 1；**主键并列时按 1/并列数折算**（3 笔等额出金时最高只有 0.33） |
| p@3 | 正确出金是否落在前 3 |
| MRR | 首个正确候选排名的倒数 |

两组基线：`仅金额匹配`（按 \|入金−出金\| 排序）、`仅时间最近`（按 \|出金时间−入金时间\| 排序）。
并列折算规则对基线与本法一视同仁，不存在只对自己有利的判法。

评估集刻意造了三种难度：等额诱饵（外部用户取出与真出金完全相同的金额且更靠近入金时间）、
时间错位（真出金的延迟被人为打乱）、结构不可解（固定面额池的多笔同面额提现）。
其中固定面额案所有方法的 p@1 都只有 0.2 —— 这是数据本身没有唯一解，不是算法缺陷，我们不给假精度。

口径铁律：本指标只在**合成数据 + 明确标注竞争条件**的前提下成立，表述为
「归因一致性 p@1（含竞争条件 · 合成数据）」。它不是实战效果，也不是案件结论。

## 6. 局限（写材料时必须带上）

1. **合成数据，不能外推真实案件。** 评估集的真值来自构造过程，诱饵、噪声、金额分布都是程序写的；
   真实混币服务的诱饵更多、更随机、还夹着跨链与交易所内部划转，本集的 p@1 不能当作真实场景的效果。
2. **阈值是启发式参数，不是标定结果。** `min_inputs=3`、`min_equal_outputs=3`、3 小时/48 小时窗口这些值
   没有用真实样本标定过；换数据必须重标，改参数不需要改代码（都在 `parameters` 里）。
3. **归因在结构上可能无解。** 固定面额池（Tornado 式）的多笔同面额提现，密码学上就不可链接；
   本算法只能给"候选 + 分数 + 理由"，不能给唯一答案。金额完全相同的候选中，
   我们靠下游是否继续流向法币出口来分辨——这是启发式，不是证明。
4. **只处理已标注的出口类型。** 出口判定依赖 `node.type` 标签与规则命中；未标注的地址不会被判为出口，
   因此会漏掉"尚未识别的兑付主体"。
5. **可控性分是不可验证的经验分。** 冻结窗口、响应时效、成本都是按出口类型推的估计值，
   没有真值可比，只能逐项复算，无法评估其对不对。
6. **没有接入真实链上数据。** 输入是约定好的 JSON 图结构；从区块浏览器/节点同步到这份结构的那一段不在本模块内。

---

参考：`docs/接口契约_混币法币识别_v0_1.md`（数据契约唯一权威）。

## 7. 两个哈希字段的区别

`input_sha256` 是对**解析后的输入对象**做规范化（键排序、无空白）哈希，同一份数据换个书写格式结果不变；
`source_file_sha256` 是对**输入文件原始字节**做哈希，入参是文件路径时自动写入、入参是对象时为 `null`——
验收要核对「同一份输入没被改过」时用后者，核对「内容语义是否一致」时用前者。
