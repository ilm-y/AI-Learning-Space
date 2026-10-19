可以，你先按下面这版审查。我们把 **Session 的定义、作用、创建时机**彻底钉死，不再往里面混别的职责。

# Session 最终定义

> **Learning Session = 围绕一个明确学习目标的一段连续学习活动。**

它位于：

```text
Learning Space
      ↓
Learning Session
      ↓
Conversation
```

三者分别是：

```text
Space
= 长期学习领域
“我在哪个领域学习？”

Session
= 阶段性学习目标
“这一阶段我要解决什么？”

Conversation
= 具体聊天线程
“我现在和 AI 聊什么？”
```

---

# 一、Session 有什么作用？

我建议只保留 **4 个核心作用**。

## 1. 表示一个“学习目标”

例如：

```text
Session
title:
理解虚拟内存地址转换

goal:
搞清楚 VA → TLB → Page Table → PA，
以及 TLB Miss 和 Page Fault 的关系
```

所以 Session 不是“这一串聊天叫什么”，而是：

> **这段学习活动最终想解决什么。**

---

## 2. 保存当前阶段性的学习焦点

例如：

```text
goal:
理解虚拟内存地址转换

current_focus:
Page Table 与 Page Fault 的关系
```

区别：

```text
goal
= 最终要解决什么

current_focus
= 当前卡在哪里 / 正在解决什么
```

这个主要给 Agent 使用。

---

## 3. 把多个 Conversation 组织成同一段学习活动

例如：

```text
Session：虚拟内存地址转换

├── Conversation A
│   TLB 是什么？
│
├── Conversation B
│   TLB Miss 发生什么？
│
├── Conversation C
│   Page Table 在哪里？
│
└── Test
    TLB Miss 一定会 Page Fault 吗？
```

所以：

```text
Space 1:N Session
Session 1:N Conversation
```

这是 Session 最重要的结构作用。

---

## 4. 作为 Agent 的“短期工作上下文”

Session 可以让 Agent 知道：

```text
这阶段用户正在解决什么目标？
现在焦点是什么？
这阶段有哪些 Conversation / Test？
```

但它**不负责长期记忆**。

长期认知仍然是：

```text
Evidence
Understanding
Understanding Revision
Memory
```

所以：

```text
Session
= 短期学习活动

Memory
= 长期用户记忆
```

---

# 二、Session 不负责什么？

这几个边界一定要记住：

```text
Session ≠ Conversation
Session ≠ Space
Session ≠ Memory
Session ≠ Understanding
Session ≠ Evidence
```

具体来说：

### Session 不保存“用户学会了什么”

→ `Learning Understanding`

### Session 不保存“为什么这么判断”

→ `Learning Evidence`

### Session 不保存长期用户偏好

→ `Memory`

### Session 不等于聊天记录

→ `Conversation`

### Session 不等于整个学科

→ `Learning Space`

---

# 三、Session 什么时候创建？

这是现在最关键的规则：

> **懒创建（Lazy Create）。**

不是：

```text
创建 Space
→ 创建 Session
```

也不是：

```text
创建 Conversation
→ 无脑创建 Session
```

而是：

```text
创建 Conversation
→ 暂时 session_id = NULL

用户发送第一条有效学习消息
→ Session Resolver
→ 决定：
   ├── 加入已有 Session
   └── 创建新 Session
```

---

# 四、为什么 Conversation 创建时不立刻创建 Session？

因为：

```text
用户点击“新建探讨”
↓
什么都没说
↓
退出
```

这时候如果已经创建：

```text
Session #37
```

你就会产生大量没有实际学习行为的垃圾 Session。

所以：

```text
Conversation 可以先存在
Session 可以暂时不存在
```

这也是 `conversation.session_id` 可以为 NULL 的**唯一合理业务状态之一**。

---

# 五、什么时候决定 Attach 还是 Create？

用户发送第一条真正的问题后：

```text
Session Resolver
```

开始工作。

输入：

```text
当前 User
当前 Space
当前 Conversation
第一条有效消息
Detected Concept
已有 ACTIVE / PAUSED Session
```

然后：

```text
┌───────────────────────┐
│ 是否存在相关学习活动？ │
└──────────┬────────────┘
           │
     ┌─────┴─────┐
     ↓           ↓
    有           没有
     ↓           ↓
  ATTACH        CREATE
```

---

## Session Resolver 的输入 / 输出契约

上面那一步是整个 Session 机制的**唯一决策点**，所以它的输入输出必须钉死，
而不是留给实现自由发挥。实现见 `backend/.../model/SessionResolver.java`
（纯函数，放 `model/` 与 `MisconceptionLifecycle` 同一层：领域规则脱离 Spring 可单测）。

### 输入

```text
detectedConcepts : List<String>   本轮消息命中的 Concept 名（LearningAnalysisService.matchConcepts
                                  的输出，可为空）
candidates       : List<Candidate> 本 user + 本 space + status ∈ (ACTIVE, PAUSED)，
                                  按 last_active_at desc
now              : LocalDateTime  参考时刻，由调用方传入
```

`now` 之所以要传进来而不是在内部取 `LocalDateTime.now()`：「最近 7 天内」是条
**可被测试断言**的规则，内部取当前时间就永远只能靠等待来测。

### 输出

```text
Decision {
  action    : ATTACH | CREATE | NONE
  sessionId : Long?      // 仅 ATTACH 时非空
  title     : String?    // 仅 CREATE 时用于建 Session
  goal      : String?    // 仅 CREATE 时用于建 Session
}
```

### 规则（按优先级，先命中先返回）

```text
1. detectedConcepts 为空
   → NONE

2. 某 candidate 的 title / goal / current_focus 文本命中任一 detectedConcept
   → ATTACH 该 candidate

3. 恰好只有一个 ACTIVE candidate，且 last_active_at 在 7 天内
   → ATTACH 它

4. 其余
   → CREATE（title / goal 由命中的 Concept 名生成）
```

### 为什么 `NONE` 是一档独立输出，而不是「没想好」

`NONE` 表示：**这次交互不产生、也不归属任何学习活动**，`conversation.session_id`
保持 `NULL`。这不是错误状态，也不是待补全的中间态 —— 普通闲聊、问一个与当前
任何学习目标都无关的新知识点，都属于这一类。

所以 `conversation.session_id = NULL` 有**两种**各自合理的业务状态：

```text
其一：用户建了 Conversation 但还没说话          （见 §四）
其二：用户说话了，但 Session Resolver 判定为 NONE （本节）
```

把 `NONE` 实现成「落到规则 4 去 CREATE」是错的：那会让每一句闲聊都生出一个
垃圾 Session，正是 §四 要防的那件事。反过来，把它实现成「不处理、保持 NULL」
却又不写进规则，则会让 NULL 变成无法解释的脏数据 —— 所以它是显式的一档。

`title` / `goal` 由命中的 Concept 名拼出，形如 `围绕「TLB、Page Table」的学习`。
这是一个**诚实下界**：系统此刻确实只知道「用户在谈这几个概念」，不该编造一个
更具体的学习目标。用户随后可以通过 `PATCH /api/sessions/{id}` 把它改写成自己的话。

---

# 六、什么情况下 Attach 到已有 Session？

例如：

当前 Space：

```text
操作系统
```

已有：

```text
Session #20
目标：理解虚拟内存地址转换
```

用户新开一个 Conversation：

> Page Table 到底在哪里？

系统发现：

```text
当前问题
+
TLB
+
Page Table
+
Page Fault
```

都和 Session #20 的目标高度相关。

那么：

```text
Conversation #101
session_id = 20
```

不创建新 Session。

结果：

```text
Session #20
├── Conversation #100
├── Conversation #101
└── Test
```

### 「高度相关」在 MVP 里怎么判定

上面那句「都和 Session #20 的目标高度相关」是个**语义**判断，而 MVP 用的是一条
**廉价的近似**（见 §五 的规则 2）：

```text
Session 的 title / goal / current_focus 文本里，是否出现过本轮命中的 Concept 名
```

选它而不是「本轮 Concept 与 Session 内已有 Evidence 的 Concept 交集」，理由是后者要
一次跨 4 张表的 join，而收益在 MVP 阶段看不出来。**代价要说清楚**：它只能命中
「目标里写到了这个概念」的情况，用户把目标写成了同义说法（goal 写「地址转换」、
消息里说「TLB」，而 TLB 恰好没被写进 goal）时，会退到规则 3 —— 靠「唯一且最近的
ACTIVE」接上；如果连这条也不满足，就会开一个新的 Session。

这条规则被刻意收在一个纯函数里（`SessionResolver`），所以以后升级成基于
Evidence 交集的判定，只需要改这一个类，两侧的调用方与数据库都不用动。

---

# 七、什么情况下 Create 新 Session？

主要三种。

### 1. 没有合适的现有 Session

比如用户第一次学习：

> TLB 是什么？

没有已有学习活动。

↓

创建：

```text
Session #20
```

---

### 2. 明显换了学习目标

当前：

```text
Session #20
目标：虚拟内存
```

用户突然：

> inode 是什么？

这已经是：

```text
文件系统
```

而不是当前目标。

可以创建：

```text
Session #21
目标：理解 inode
```

---

### 3. 用户明确要求开始新的学习目标

以后可以提供：

> 开始新的学习目标

这时直接创建新 Session。

第一版 UI 甚至可以不提供这个按钮，先靠系统规则完成。

---

# 八、Session 什么时候结束？

**不是按时间结束。**

比如：

```text
9月1日
9月3日
9月8日
9月10日
```

都可以属于同一个 Session。

因为 Session 的边界是：

> **学习目标**

而不是：

> **时间。**

---

# 九、Session 的状态

我建议：

```text
ACTIVE
PAUSED
COMPLETED
ABANDONED
```

### ACTIVE

当前正在进行。

### PAUSED

暂时停下，但这个学习目标还没完成。

比如一周没学，不应该自动认为“学完了”。

### COMPLETED

学习目标已经完成。

例如：

```text
理解虚拟内存地址转换
```

已经达到目标。

### ABANDONED

用户明确放弃这条学习目标。

---

# 十、特别重要：Session 完成以后，认知不会消失

例如：

```text
Session #20
目标：虚拟内存
COMPLETED
```

里面产生：

```text
Evidence
Understanding
Revision
Memory
```

这些继续存在。

一个月后：

> TLB 我又忘了。

不会直接复活 Session #20。

而可以：

```text
Session #35
目标：复习虚拟内存
```

然后新 Session 通过：

```text
Understanding
+
Memory
+
Evidence
```

知道：

> 用户以前已经学过，只是现在回来复习。

这正是：

```text
Session = 活动
Memory = 长期
```

的意义。

---

# 十一、所以整个生命周期就是

```text
                  创建 Conversation
                         │
                         ↓
                  session_id = NULL
                         │
                         ↓
                用户发送有效问题
                         │
                         ↓
                  Session Resolver
                     /          \
                    /            \
             有相关 Session      没有
                  ↓               ↓
                ATTACH          CREATE
                  \               /
                   \             /
                    ↓           ↓
                     ACTIVE
                        │
                        ↓
                多个 Conversation
                        │
                        ↓
             Evidence / Understanding
                        │
                        ↓
                    学习继续
                        │
             ┌──────────┼──────────┐
             ↓          ↓          ↓
           PAUSED   COMPLETED   ABANDONED
```

---

# 十二、最后把数据库关系定成一句话

```text
USER
  ↓
LEARNING_SPACE
  ↓
LEARNING_SESSION
  ↓
CONVERSATION
  ↓
MESSAGE
```

其中：

```text
Conversation.session_id = NULL
```

表示：

> 这个 Conversation 还没有被归入具体学习活动。

一旦第一条有效学习消息出现：

```text
Session Resolver
      ↓
Attach Existing
      or
Create New
```

---

# 最终审查版

你可以直接拿下面 6 句话审：

> **1. Space 是长期学习领域。**

> **2. Session 是围绕一个明确学习目标的一段连续学习活动。**

> **3. Conversation 是具体聊天线程，一个 Session 可以包含多个 Conversation。**

> **4. Session 主要负责学习目标、当前焦点、组织多个 Conversation，以及提供短期 Agent 工作上下文。**

> **5. Session 不承载长期 Understanding、Evidence 或 Memory。**

> **6. Session 采用懒创建：Conversation 创建时可以 `session_id = NULL`，用户产生第一条有效学习消息后，由 Session Resolver 决定加入现有 Session 或创建新 Session。**

我认为这 6 条现在已经足够作为你 `PRD` 和 `DATABASE_AND_API` 里的正式设计规则。