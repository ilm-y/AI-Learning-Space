# Phase 4.2 Spring SSE 验收

- 后端：`http://127.0.0.1:18080`
- Space：`18`，Conversation：`3`
- ready ids：`[64, 65, 66, 67, 68, 69, 70]`
- 总体：未通过

| 用例 | 问题 | 通过 | 事件数 | delta 数 | 引用数 | error |
| --- | --- | --- | ---: | ---: | ---: | --- |
| S01 | ZenFrame 的提醒模块通过什么协议发送云端定时提醒？ | 是 | 12 | 10 | 1 | 无 |
| S02 | ZealDeck 的离线提醒最多缓存多少条？ | 是 | 10 | 8 | 1 | 无 |
| S03 | 请比较 ZenFrame 和 ZealDeck 的近场传输技术和传输上限。 | 是 | 78 | 76 | 2 | 无 |
| S04 | 请解释量子色动力学中的渐进自由。 | 否 | 172 | 170 | 0 | 无 |

## 逐项检查

### S01

回答：

```text
ZenFrame 的提醒模块通过 **MQTT 3.1.1** 协议发送云端定时提醒。

> 来源：[资料《zenframe.md》#0]
```

- sse_format：通过
- start_first：通过
- end_last：通过
- no_message_error：通过
- delta_fields：通过
- answer_not_empty：通过
- required_terms：通过（['MQTT']）
- required_citation_documents：通过（['zenframe.md']）
- no_malformed_citations：通过

### S02

回答：

```text
ZealDeck 的离线提醒最多缓存 **5 条**。

> 来源：[资料《zealdeck.md》#0]
```

- sse_format：通过
- start_first：通过
- end_last：通过
- no_message_error：通过
- delta_fields：通过
- answer_not_empty：通过
- required_terms：通过（['5']）
- required_citation_documents：通过（['zealdeck.md']）
- no_malformed_citations：通过

### S03

回答：

```text
以下是 ZenFrame 和 ZealDeck 在**近场图片传输**方面的关键对比（仅基于你提供的设备说明）：

| 维度             | ZenFrame                          | ZealDeck                           |
|------------------|-------------------------------------|--------------------------------------|
| **传输技术**     | 星闪 SLE（SparkLink Low Energy）    | 蓝牙 BLE 5.3                         |
| **单次传输上限** | **8 MiB**                           | **4 MiB**                            |
| **能力协商**     | 传输前交换能力参数                  | 传输前交换能力参数                   |
| **降级行为**     | ❌ 不支持降级到蓝牙                 | ❌ 不支持降级到星闪 SLE              |
| **低电量影响**   | 关闭星闪 SLE，近场传输暂停          | 关闭蓝牙 BLE，近场传输暂停           |

📌 补充说明：  
- 两者都**不自动降级**——若对端不支持本设备的近场协议，传输直接失败，不会尝试 fallback 到另一方可能支持的协议（例如 ZenFrame 不会试 BLE，ZealDeck 也不会试 SLE）。  
- 近场传输功能在低电量模式下均被主动关闭（[资料《zenframe.md》#2]、[资料《zealdeck.md》#2]），但云端提醒链路（MQTT / Kafka）仍保持心跳，不影响离线缓存与后续补发。

需要我帮你分析「为什么选 SLE 而不是 BLE？」或「8 MiB vs 4 MiB 在实际使用中意味着什么？」，可以继续问。
```

- sse_format：通过
- start_first：通过
- end_last：通过
- no_message_error：通过
- delta_fields：通过
- answer_not_empty：通过
- required_terms：通过（['星闪', 'BLE', '8', '4']）
- required_citation_documents：通过（['zenframe.md', 'zealdeck.md']）
- no_malformed_citations：通过

### S04

回答：

```text
量子色动力学（QCD）中的**渐进自由**（Asymptotic Freedom）是指：**夸克之间的强相互作用力，随着它们彼此距离减小（或能量尺度升高），反而变弱；反之，当它们被拉开时，力变得极强，导致禁闭。**

这不是直觉上的“越靠近越强”（如电磁力或引力），而是 QCD 独有的反常行为——它源于胶子（gluon）自身的色荷（color charge）以及由此产生的**反屏蔽效应**。

---

### 🔍 关键机制：胶子自相互作用 vs 光子无电荷

- 在量子电动力学（QED）中，光子不带电荷，不能自相互作用；真空极化产生**电荷屏蔽**：远处看，电子的有效电荷被虚正负电子对“包裹”，显得更小 → **耦合常数随能量升高而增大**（即红外自由、紫外发散）。
  
- 在 QCD 中，胶子携带**色荷**（红-反蓝等），因此能直接相互作用（3 胶子顶点、4 胶子顶点）。这导致真空极化以相反方式响应：
  - 高能（短距）探针看到的不是屏蔽，而是**反屏蔽**（antiscreening）：虚胶子云增强而非削弱中心夸克的色场。
  - 结果：**有效色耦合强度 αₛ(Q²) 随动量转移 Q² 增大而减小**。

数学上，QCD β 函数在单圈近似下为：

\[
\beta(\alpha_s) = \mu \frac{d\alpha_s}{d\mu} = -\frac{\beta_0}{2\pi} \alpha_s^2 + \mathcal{O}(\alpha_s^3), \quad \text{其中 } \beta_0 = 11 - \frac{2}{3}n_f
\]

当夸克味数 \(n_f < 16.5\)（实际 \(n_f = 6\)），\(\beta_0 > 0\)，β 函数为负 → αₛ 随能量升高而**减小** → 渐进自由成立。

---

### 🌐 物理含义与实验证据

- ✅ **高能散射中夸克表现得像自由粒子**：深度非弹性散射（DIS）中，电子-质子碰撞在高 Q² 下显示夸克近乎无相互作用（Bjorken 标度无关性），是渐进自由的直接证据（1990 年代 Nobel 奖核心依据）。
- ⚠️ **但绝不意味着“夸克可孤立存在”**：因为 αₛ 在低能（~1 GeV 以下）急剧增大 → 势能 \(V(r) \sim \kappa r\)（线性禁闭势），拉伸时能量足以产生新夸克对 → 强子化（hadronization）。

> 所以，“自由”仅在**渐近高能极限**（\(Q^2 \to \infty\)）下成立；现实中，我们永远观测不到裸夸克——这是渐进自由与夸克禁闭（confinement）共存的深刻统一。

需要我用类比图示、数值例子（比如 αₛ 在 100 GeV vs 1 GeV 的典型值），或联系 LHC 上喷注（jet）物理来说明它的实际影响吗？
```

- sse_format：通过
- start_first：通过
- end_last：通过
- no_message_error：通过
- delta_fields：通过
- answer_not_empty：通过
- required_terms：通过
- required_citation_documents：通过
- no_malformed_citations：通过
- no_fabricated_citation：通过
- admits_missing_document：失败（{'patterns': ['(?:资料|文档|上传)[^。！？\\n]{0,15}(?:没有|未|不)(?:直接|相关|命中|检索到|找到|包含|提供|提及|覆盖)', '(?:没有|未)(?:找到|检索到|命中|检索出)[^。！？\\n]{0,15}(?:资料|文档|内容|相关|依据)', '没有直接答案'], 'found': []}）
