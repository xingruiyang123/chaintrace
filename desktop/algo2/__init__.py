"""链迹核验 · 混币—法币出口识别算法包（纯标准库，Python 3.13）。

对外入口：
    from algo2 import pipeline
    result, raw = pipeline.run_file('离线原型/样例/混币出口虚拟样例.json')

模块划分：
    graph.py          输入读取与结构校验（Decimal 金额、UTC 时间、is_simulated 校验）
    mixer_detect.py   混币池识别（M1–M7 七个信号，命中/未命中都输出）
    attribution.py    断链归因（六维加权，rank + tier + 人读解释）
    offramp.py        法币出口识别（规则库驱动，S_off = min(cap, Σ 规则分)）
    controllability.py 可控性评分（S_profile 五维 + S_ctrl，信号灯）
    paths.py          追缴路径方案（主路径 + 备选）
    pipeline.py       串起来 + 输出结构自检
    generate_fixture.py  生成含竞争条件的评估数据集
    benchmark.py      在评估集上算 p@1 / p@3 / MRR，并与两个基线对比

全部数据为合成数据，不接入真实案件。
"""

__all__ = [
    "graph",
    "mixer_detect",
    "attribution",
    "offramp",
    "controllability",
    "paths",
    "pipeline",
]
