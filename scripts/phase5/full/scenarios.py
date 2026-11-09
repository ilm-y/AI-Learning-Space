"""Phase 5 Step 3 全量 A/B/C 对照实验的 10 个场景定义。

每个场景拥有独立的 Space（因此 Knowledge 文档天然按 Space 隔离）、
Concept、Session 与 Conversation。所有场景共用同一个用户、同一个隔离
MySQL 库与同一个 Qdrant collection 前缀，因此属于「同一个快照环境」。

定义只包含实验输入，不包含任何密钥。所有标记（【Sx-...】）都只用于
验证「模型是否真的使用了预置上下文」，不会写进生产代码。
"""

from __future__ import annotations

from typing import Any

USER_PASSWORD = "phase5-full-only"  # 隔离测试用户口令；不用于任何真实账号

# 每个场景的公共结构：
#   id / name / space / concepts / sessions / conversations / currentConversation
#   question / understanding / evidence / revisions / currentRevisionNo / memory
#   document / expect / notes
def _s10_history() -> list[list[str]]:
    turns = [
        (
            "【S10-H1】我先把文件系统的层次理一遍：VFS、具体文件系统、"
            "块设备层，各自负责什么？",
            "【S10-A1】VFS 提供统一接口，具体文件系统实现 inode/目录操作，"
            "块设备层负责把逻辑块读写落到设备。",
        ),
        (
            "【S10-H2】inode 里到底有哪些字段？为什么文件名不放在 inode 里？",
            "【S10-A2】inode 保存类型、权限、大小、时间戳和数据块指针；"
            "文件名放在目录项里，这样硬链接可以共享同一个 inode。",
        ),
        (
            "【S10-H3】目录项和 inode 的关系我还是有点乱，目录本身也是文件吗？",
            "【S10-A3】是，目录也是一种文件，内容是一组“名字 → inode 号”的映射。",
        ),
        (
            "【S10-H4】页缓存和文件系统的关系是什么？read 是不是一定读磁盘？",
            "【S10-A4】read 先查页缓存，命中就直接返回；未命中才发起块设备读，"
            "并可能触发预读。",
        ),
        (
            "【S10-H5】write 之后数据什么时候真正落盘？和 fsync 有什么关系？",
            "【S10-A5】write 通常只更新页缓存，fsync 才要求把数据和必要的元数据"
            "刷到持久存储。",
        ),
        (
            "【S10-H6】如果 write 之后、fsync 之前崩溃，数据会怎样？",
            "【S10-A6】可能丢失最近写入；文件系统一致性由日志或写时复制等机制"
            "保证，但已确认持久化的语义要靠 fsync。",
        ),
        (
            "【S10-H7】日志文件系统是不是所有数据都写两遍？性能不会很差吗？",
            "【S10-A7】常见模式只记录元数据（data=ordered/writeback），"
            "数据块不一定写两遍；data=journal 才把数据也写进日志。",
        ),
        (
            "【S10-H8】checkpoint 是什么？它和日志提交有什么区别？",
            "【S10-A8】提交是把事务写进日志并标记完整；checkpoint 是把日志中的"
            "更新写回真正的元数据位置。",
        ),
        (
            "【S10-H9】崩溃恢复时怎么判断一条日志事务是完整的？",
            "【S10-A9】看日志记录的事务边界和校验信息；完整提交的重放，"
            "未提交的丢弃。",
        ),
        (
            "【S10-H10】写屏障为什么重要？没有它先写日志也可能失效吗？",
            "【S10-A10】设备可能重排写入；写屏障保证日志数据先落盘，"
            "否则恢复时可能看到“日志未完整但数据已部分写回”。",
        ),
        (
            "【S10-H11】ext4 的 ordered 模式和 writeback 模式差别在哪？",
            "【S10-A11】ordered 保证数据块先于元数据提交写盘；writeback 只保证"
            "元数据顺序，数据一致性依赖更弱的假设。",
        ),
        (
            "【S10-H12】如果日志区满了会发生什么？会不会阻塞所有写操作？",
            "【S10-A12】日志空间不足时文件系统会触发 checkpoint 或等待，"
            "严重时写操作会被阻塞；这是日志空间规划要关注的点。",
        ),
        (
            "【S10-H13】SSD 上日志还有意义吗？掉电语义和机械盘一样吗？",
            "【S10-A13】有意义；SSD 也有易失缓存和重排，掉电语义取决于"
            "设备缓存策略和写屏障。",
        ),
        (
            "【S10-H14】总结一下：日志保证一致性，但能不能保证数据不丢？",
            "【S10-A14】日志主要保证元数据一致性；数据是否丢失取决于 fsync、"
            "日志模式和设备持久化语义，两者不能混为一谈。",
        ),
    ]
    history: list[list[str]] = []
    for user, assistant in turns:
        history.append(["USER", user])
        history.append(["ASSISTANT", assistant])
    return history


def _s10_known() -> list[str]:
    return [
        "VFS 提供统一文件接口，具体文件系统实现 inode 与目录操作",
        "inode 保存元数据与数据块指针，文件名保存在目录项中",
        "目录本身也是文件，内容是名字到 inode 号的映射",
        "页缓存会缓存文件页，read 命中时不需要访问磁盘",
        "write 通常只更新页缓存，fsync 才要求持久化",
        "日志文件系统先写日志，提交后再 checkpoint 到元数据位置",
        "崩溃恢复根据日志事务边界决定重放或丢弃",
        "写屏障保证日志数据先于元数据落盘，避免设备重排破坏顺序",
        "ext4 ordered 模式保证数据块先于元数据提交",
        "日志空间不足会触发 checkpoint 或阻塞写操作",
        "SSD 同样需要关注易失缓存与写屏障",
        "日志保证一致性，但不等于保证数据不丢失",
    ]


def _s10_uncertain() -> list[str]:
    return [
        "data=journal 与 data=ordered 在崩溃恢复时的具体差异",
        "checkpoint 与日志提交的先后关系在极端并发下的表现",
        "多级日志或外部日志设备对恢复时间的影响",
        "fsync 与 fdatasync 在元数据持久化上的边界",
        "日志校验和损坏时文件系统的恢复策略",
        "写时复制文件系统与日志文件系统的一致性模型差异",
    ]



SCENARIOS: list[dict[str, Any]] = [
    {
        "id": "S1",
        "name": "普通知识问答",
        "space": {
            "name": "S1 网络拥塞",
            "description": "普通知识问答：问题本身不点名概念，历史里出现过概念",
        },
        "concepts": [
            {
                "key": "main",
                "canonical": "TCP 拥塞控制",
                "display": "TCP 拥塞控制",
                "description": "TCP 通过拥塞窗口、慢启动与拥塞避免调节发送速率",
                "difficulty": 2,
            }
        ],
        "sessions": [
            {
                "key": "s1",
                "title": "TCP 拥塞控制：从丢包到拥塞窗口",
                "goal": "理解 TCP 拥塞控制为什么需要慢启动和拥塞避免",
                "focus": "说不清拥塞窗口和丢包之间的关系",
            }
        ],
        "conversations": [
            {
                "key": "current",
                "title": "S1 普通知识问答",
                "session": "s1",
                "history": [
                    [
                        "USER",
                        "【S1-HISTORY】我原来以为 TCP 拥塞控制只是带宽不够的问题，"
                        "和发送端的行为无关。",
                    ],
                    [
                        "ASSISTANT",
                        "【S1-ASSIST】带宽只是容量上限；TCP 的拥塞窗口会主动探测"
                        "可用带宽，避免把网络推到崩溃。",
                    ],
                ],
            }
        ],
        "currentConversation": "current",
        "question": "为什么数据一多，网络就会变慢甚至丢包？",
        "understanding": {
            "concept": "main",
            "summary": "【S1-U】用户知道拥塞是流量超过链路容量，但仍把原因主要归为带宽不足。",
            "known": ["知道带宽是链路的容量上限"],
            "misconceptions": [],
            "uncertain": ["TCP 拥塞窗口如何影响拥塞"],
            "explanation": "【S1-U】带宽不够就会拥塞。",
            "status": "PARTIAL",
        },
        "evidence": {
            "concept": "main",
            "observation": "【S1-E】用户尚未把 TCP 发送行为与拥塞联系起来。",
            "correctness": "PARTIAL",
        },
        "revisions": [
            {
                "no": 1,
                "summary": "【S1-R1】用户把拥塞主要归因于带宽不足。",
                "explanation": "【S1-R1】带宽不够就会拥塞。",
            }
        ],
        "currentRevisionNo": 1,
        "memory": {
            "concept": "main",
            "content": "【S1-M】用户偏好用带宽和队列长度理解 TCP 拥塞，"
            "容易忽略拥塞窗口的作用。",
            "importance": 0.8,
        },
        "document": {
            "name": "s1-tcp-congestion.md",
            "text": """# S1 TCP 拥塞控制

网络拥塞不是单一原因造成的。链路的带宽是容量上限，但 TCP 发送端
不知道网络当前能承受多少流量；如果多个连接同时加速，路由器队列会
被填满，随后出现丢包和排队时延上升。

## 拥塞窗口与慢启动

TCP 为每个连接维护拥塞窗口（cwnd）。慢启动阶段 cwnd 从 1 个 MSS
开始，每收到一个 ACK 就增长，因此呈指数上升；到达慢启动阈值后进入
拥塞避免，改为每个 RTT 线性增长。这样发送端是在“试探”可用带宽，
而不是等带宽耗尽后才反应。

## 为什么会出现拥塞

当所有发送端都按可用带宽估计加速，而网络总容量有限，聚合流量会超过
链路和路由器队列的承受能力。丢包、超时或显式拥塞信号出现后，TCP
才回退 cwnd。因此拥塞的根源是“聚合发送速率超过网络承载能力”，
带宽不足只是其中一个条件，发送端的行为同样关键。
""",
        },
        "expect": {
            "C_intent": "LEARNING_EXPLANATION",
            "C_must_include": ["knowledge", "understanding", "memory", "evidence", "session"],
            "C_must_exclude": [],
            "C_session": True,
            "B_session": True,
            "B_must_include": ["knowledge", "understanding", "memory", "history"],
            "A_must_exclude": [
                "understanding",
                "memory",
                "evidence",
                "trajectory",
                "session",
                "history",
            ],
            "degraded_cognitive": False,
        },
        "notes": (
            "A 清空 history/session 后问题本身不命中概念，退化为 KNOWLEDGE_QUERY，"
            "只注入 Knowledge；B/C 能从历史里恢复 TCP 概念，注入个人上下文。"
            "这是 A 与 B/C 在普通问答中的非干扰性差异。"
        ),
    },
    {
        "id": "S2",
        "name": "同一 Session 连续学习",
        "space": {
            "name": "S2 B+ 树连续学习",
            "description": "同一 Session 内的连续追问",
        },
        "concepts": [
            {
                "key": "main",
                "canonical": "B+ 树",
                "display": "B+ 树",
                "description": "B+ 树是多路平衡搜索树，叶节点保存数据并通过链表连接",
                "difficulty": 3,
            }
        ],
        "sessions": [
            {
                "key": "s2",
                "title": "B+ 树：叶子节点与范围查询",
                "goal": "掌握 B+ 树的叶子节点与范围查询",
                "focus": "不清楚叶子链表如何支持范围扫描",
            }
        ],
        "conversations": [
            {
                "key": "current",
                "title": "S2 同一 Session 连续学习",
                "session": "s2",
                "history": [
                    [
                        "USER",
                        "【S2-HISTORY】B+ 树的内部节点只存索引，数据都在叶子上，"
                        "这点我理解了。",
                    ],
                    [
                        "ASSISTANT",
                        "【S2-ASSIST】对；叶子节点还通过链表连接，"
                        "范围查询才能顺序扫描。",
                    ],
                ],
            }
        ],
        "currentConversation": "current",
        "question": "继续讲：B+ 树的范围查询为什么比哈希索引快？",
        "understanding": {
            "concept": "main",
            "summary": "【S2-U】用户理解 B+ 树数据在叶子、叶子有链表；"
            "还说不清范围查询的磁盘 IO 优势。",
            "known": ["内部节点只存索引", "叶子节点保存数据", "叶子节点有链表"],
            "misconceptions": [],
            "uncertain": ["范围查询为什么能减少随机 IO"],
            "explanation": "【S2-U】B+ 树的叶子在最后一层，叶子之间有链表。",
            "status": "PARTIAL",
        },
        "evidence": {
            "concept": "main",
            "observation": "【S2-E】用户能复述结构，但未解释范围查询的磁盘 IO 优势。",
            "correctness": "PARTIAL",
        },
        "revisions": [
            {
                "no": 1,
                "summary": "【S2-R1】用户已掌握内部节点与叶子节点的分工。",
                "explanation": "【S2-R1】数据都在叶子上。",
            }
        ],
        "currentRevisionNo": 1,
        "memory": {
            "concept": "main",
            "content": "【S2-M】用户喜欢用磁盘页和顺序 IO 解释 B+ 树，"
            "连续追问时希望接着上一轮的叶子链表讲。",
            "importance": 0.85,
        },
        "document": {
            "name": "s2-bplus-range.md",
            "text": """# S2 B+ 树与范围查询

B+ 树把所有数据记录放在叶子节点，内部节点只保存用于导航的键和
子节点指针。叶子节点按顺序通过双向链表连接，因此范围查询可以先
定位到起始叶子，再沿链表顺序扫描后续叶子。

## 为什么范围查询比哈希索引快

哈希索引擅长等值查询：给定完整键可以直接算出桶位置。但哈希索引
不保存键的顺序，范围查询只能逐值查询或全表扫描。B+ 树的范围查询
代价是：一次树高 O(log n) 的定位，加上按链表顺序读取 k 个叶子页，
整体接近顺序 IO。对磁盘和 SSD 来说，顺序读取通常比 k 次随机定位
更便宜，也更适合预读。

## 等值查询的差异

等值查询下哈希索引通常 O(1)，B+ 树是 O(log n)。B+ 树的优势在于
范围、排序和前缀查询，以及稳定的磁盘页访问模式；这也是数据库
默认使用 B+ 树作为主索引结构的原因之一。
""",
        },
        "expect": {
            "C_intent": "CONTINUE_TASK",
            "C_must_include": ["knowledge", "understanding", "memory", "session"],
            "C_must_exclude": [],
            "C_session": True,
            "B_session": True,
            "B_must_include": ["knowledge", "understanding", "memory", "history"],
            "A_must_exclude": [
                "understanding",
                "memory",
                "evidence",
                "trajectory",
                "session",
                "history",
            ],
            "degraded_cognitive": False,
        },
        "notes": "「继续」+ 会话概念匹配触发 CONTINUE_TASK；C 注入 Session goal/focus。",
    },
    {
        "id": "S3",
        "name": "跨 Session 学习",
        "space": {
            "name": "S3 跨 Session B+ 树",
            "description": "不同 Session 之间共享同一 Concept 的学习记录",
        },
        "concepts": [
            {
                "key": "main",
                "canonical": "B+ 树",
                "display": "B+ 树",
                "description": "B+ 树是多路平衡搜索树，叶节点保存数据并通过链表连接",
                "difficulty": 3,
            }
        ],
        "sessions": [
            {
                "key": "old",
                "title": "B+ 树基础：分裂与合并",
                "goal": "理解 B+ 树的分裂与合并",
                "focus": "分裂时中间元素如何上移",
            },
            {
                "key": "current",
                "title": "B+ 树范围查询：叶子链表",
                "goal": "理解叶子链表与范围扫描",
                "focus": "叶子链表如何支持范围查询",
            },
        ],
        "conversations": [
            {
                "key": "old",
                "title": "S3 上一阶段对话",
                "session": "old",
                "history": [
                    [
                        "USER",
                        "【S3-OLD-HISTORY】B+ 树分裂时，中间元素是不是要上移到父节点？",
                    ],
                    [
                        "ASSISTANT",
                        "【S3-OLD-ASSIST】对，分裂把中间键上移，左右两半成为两个子节点。",
                    ],
                ],
            },
            {
                "key": "current",
                "title": "S3 当前阶段对话",
                "session": "current",
                "history": [
                    [
                        "USER",
                        "【S3-HISTORY】我们上个阶段把 B+ 树的分裂与合并讲完了，"
                        "今天看范围查询。",
                    ],
                    [
                        "ASSISTANT",
                        "【S3-ASSIST】好，先回忆叶子节点之间的链表。",
                    ],
                ],
            },
        ],
        "currentConversation": "current",
        "question": "B+ 树的叶子节点链表有什么作用？",
        "understanding": {
            "concept": "main",
            "summary": "【S3-U】用户在上一阶段理解了分裂与合并，也记住了叶子链表；"
            "但还没把链表和范围扫描联系起来。",
            "known": ["分裂与合并", "叶子节点有链表"],
            "misconceptions": [],
            "uncertain": ["叶子链表如何支持范围扫描"],
            "explanation": "【S3-U】分裂时中间元素上移。",
            "status": "PARTIAL",
        },
        "evidence": {
            "concept": "main",
            "observation": "【S3-E】上一会话记录：用户能解释分裂，未解释范围扫描。",
            "correctness": "PARTIAL",
        },
        "revisions": [
            {
                "no": 1,
                "summary": "【S3-R1】用户在上一阶段掌握了分裂与合并。",
                "explanation": "【S3-R1】分裂时中间元素上移。",
            }
        ],
        "currentRevisionNo": 1,
        "memory": {
            "concept": "main",
            "content": "【S3-M】用户习惯把 B+ 树和磁盘页大小一起考虑；"
            "跨阶段学习时希望先回忆上一阶段的结论。",
            "importance": 0.8,
        },
        "document": {
            "name": "s3-bplus-leaf.md",
            "text": """# S3 B+ 树叶子链表

B+ 树的叶子节点保存实际数据记录，并按键值顺序排列。相邻叶子节点
之间用链表连接，通常还是双向链表。这个链表不是装饰：它把“树”
的随机定位转换成“定位一次 + 顺序扫描”。

## 范围查询的执行过程

给定范围 [a, b)，B+ 树先从根向下定位到第一个键不小于 a 的叶子，
然后在叶子链表上顺序向后扫描，直到键超过 b。每个叶子页只访问一次，
并且相邻叶子在磁盘上往往可以预读。

## 与分裂合并的关系

分裂发生时，中间键上移到父节点，左右两半成为新的子节点；叶子层
的链表指针也要随分裂更新。合并时则相反。理解分裂与合并，有助于
理解为什么叶子链表在结构变化后仍然保持有序。
""",
        },
        "expect": {
            "C_intent": "LEARNING_EXPLANATION",
            "C_must_include": ["knowledge", "understanding", "memory", "session"],
            "C_must_exclude": [],
            "C_session": True,
            "B_session": True,
            "B_must_include": ["knowledge", "understanding", "memory", "history"],
            "A_must_exclude": [
                "understanding",
                "memory",
                "evidence",
                "trajectory",
                "session",
                "history",
            ],
            "degraded_cognitive": False,
        },
        "notes": "个人上下文来自上一 Session；当前 Session 只提供 goal/focus。",
    },
    {
        "id": "S4",
        "name": "跨 Conversation 同 Concept（TLB）",
        "space": {
            "name": "S4 TLB 跨会话",
            "description": "A 错误、B 纠正、C 切 Redis、D 重新问 TLB",
        },
        "concepts": [
            {
                "key": "main",
                "canonical": "TLB",
                "display": "TLB（快表）",
                "description": "Translation Lookaside Buffer，缓存最近使用的页表项",
                "difficulty": 3,
            }
        ],
        "sessions": [
            {
                "key": "a",
                "title": "TLB 初学：A 会话",
                "goal": "理解 TLB 与页表的关系",
                "focus": "TLB 到底缓存什么",
            },
            {
                "key": "b",
                "title": "TLB 纠错：B 会话",
                "goal": "纠正 TLB 缓存对象的错误理解",
                "focus": "TLB 缓存的是页表项",
            },
            {
                "key": "c",
                "title": "Redis 缓存：C 会话",
                "goal": "理解 Redis 缓存穿透",
                "focus": "布隆过滤器与空值缓存",
            },
            {
                "key": "d",
                "title": "TLB 复问：D 会话",
                "goal": "用比喻理解 TLB",
                "focus": "TLB 与页表的分工",
            },
        ],
        "conversations": [
            {
                "key": "a",
                "title": "S4-A 错误理解",
                "session": "a",
                "history": [
                    [
                        "USER",
                        "【S4-A-ERROR】TLB 就是 CPU 里的数据缓存，和页表没关系。",
                    ],
                    [
                        "ASSISTANT",
                        "【S4-A-ASSIST】这个说法不准确，TLB 缓存的是页表项。",
                    ],
                ],
            },
            {
                "key": "b",
                "title": "S4-B 纠正",
                "session": "b",
                "history": [
                    [
                        "USER",
                        "【S4-B-CORRECT】我纠正一下：TLB 缓存的是最近用到的页表项，"
                        "不是普通数据。",
                    ],
                    [
                        "ASSISTANT",
                        "【S4-B-ASSIST】对，TLB 是页表的缓存，"
                        "命中就不用再走多级页表。",
                    ],
                ],
            },
            {
                "key": "c",
                "title": "S4-C 切到 Redis",
                "session": "c",
                "history": [
                    [
                        "USER",
                        "【S4-C-REDIS】先换个话题，Redis 缓存穿透怎么处理？",
                    ],
                    [
                        "ASSISTANT",
                        "【S4-C-ASSIST】可以用布隆过滤器或缓存空值。",
                    ],
                ],
            },
            {
                "key": "d",
                "title": "S4-D 重新问 TLB",
                "session": "d",
                "history": [
                    [
                        "USER",
                        "【S4-D-REASK】我们回到 TLB，我上次用的比喻是小托盘。",
                    ],
                    [
                        "ASSISTANT",
                        "【S4-D-ASSIST】好，沿用你的小托盘比喻。",
                    ],
                ],
            },
        ],
        "currentConversation": "d",
        "question": "TLB 是什么？请沿用我常用的那个比喻来说明。",
        "understanding": {
            "concept": "main",
            "summary": "【S4-U】用户已纠正：TLB 缓存最近用到的页表项；"
            "常用比喻是“鞋柜门口的小托盘”，只放最近穿的鞋。",
            "known": ["TLB 缓存页表项", "TLB 命中可以跳过页表遍历"],
            "misconceptions": [],
            "uncertain": ["TLB miss 后如何回页表"],
            "explanation": "【S4-U】TLB 像鞋柜门口的小托盘，只放最近穿的鞋。",
            "status": "PARTIAL",
        },
        "evidence": {
            "concept": "main",
            "observation": "【S4-E】用户在 B 会话完成了对 A 会话错误理解的纠正。",
            "correctness": "PARTIAL",
        },
        "revisions": [
            {
                "no": 1,
                "summary": "【S4-R1】用户认为 TLB 是 CPU 的数据缓存。",
                "explanation": "【S4-R1】TLB 就是 CPU 里的数据缓存。",
            },
            {
                "no": 2,
                "summary": "【S4-R2】用户纠正为 TLB 缓存页表项，并用小托盘作比喻。",
                "explanation": "【S4-R2】TLB 像鞋柜门口的小托盘，只放最近穿的鞋。",
            },
        ],
        "currentRevisionNo": 2,
        "memory": {
            "concept": "main",
            "content": "【S4-M】用户用“鞋柜门口的小托盘”比喻 TLB，"
            "偏好用比喻理解缓存；容易把 TLB 和数据缓存混淆。",
            "importance": 0.9,
        },
        "document": {
            "name": "s4-tlb.md",
            "text": """# S4 TLB（Translation Lookaside Buffer）

TLB 是 CPU 内存管理单元中的一个小型高速缓存，缓存的是最近使用过的
页表项（页号到物理页框号的映射），不是普通数据缓存。进程访问虚拟
地址时，MMU 先查 TLB；命中就直接得到物理页框号，未命中才去遍历
多级页表并把结果填回 TLB。

## 为什么需要 TLB

页表通常有多级，一次未命中的地址转换可能需要多次内存访问。TLB
把最近使用的页表项放在靠近 CPU 的位置，使绝大多数地址转换只需
一次 TLB 查找。TLB 很小，因此只保留最近使用的少数页表项。

## 与数据缓存的区别

数据缓存缓存的是指令或数据内容，TLB 缓存的是地址转换结果。两者
都可能 miss，但 TLB miss 的代价是走页表遍历，数据 cache miss 的
代价是从下一级存储取数据。把 TLB 说成“数据缓存”会掩盖这个区别。
""",
        },
        "expect": {
            "C_intent": "LEARNING_EXPLANATION",
            "C_must_include": ["knowledge", "understanding", "memory", "session"],
            "C_must_exclude": [],
            "C_session": True,
            "B_session": True,
            "B_must_include": ["knowledge", "understanding", "memory", "history"],
            "A_must_exclude": [
                "understanding",
                "memory",
                "evidence",
                "trajectory",
                "session",
                "history",
            ],
            "degraded_cognitive": False,
            "answer_markers_BC": ["小托盘"],
            "answer_markers_A_absent": ["小托盘"],
        },
        "notes": (
            "TLB 案例：A 错误、B 纠正、C 切 Redis、D 重新问。"
            "当前问题不触发复盘词；个人上下文来自 B 会话的纠正结果。"
            "「小托盘」只存在于 U/M，A 无法看到，是可判定的历史使用标记。"
        ),
    },
    {
        "id": "S5",
        "name": "历史错误回顾",
        "space": {
            "name": "S5 慢启动复盘",
            "description": "显式复盘意图，要求引用用户原话",
        },
        "concepts": [
            {
                "key": "main",
                "canonical": "TCP 慢启动",
                "display": "TCP 慢启动",
                "description": "TCP 慢启动从较小的拥塞窗口开始指数增长",
                "difficulty": 2,
            }
        ],
        "sessions": [
            {
                "key": "s5",
                "title": "TCP 慢启动：错在哪",
                "goal": "理解 TCP 慢启动与拥塞避免",
                "focus": "慢启动的指数增长何时停止",
            }
        ],
        "conversations": [
            {
                "key": "current",
                "title": "S5 历史错误回顾",
                "session": "s5",
                "history": [
                    [
                        "USER",
                        "【S5-HISTORY】我原来以为慢启动慢是因为它每次只把窗口加一。",
                    ],
                    [
                        "ASSISTANT",
                        "【S5-ASSIST】其实慢启动是指数增长，慢在起点小。",
                    ],
                ],
            }
        ],
        "currentConversation": "current",
        "question": (
            "回顾一下，我之前对 TCP 慢启动的理解错在哪？请引用我当时的原话。"
        ),
        "understanding": {
            "concept": "main",
            "summary": "【S5-U】用户原先误以为慢启动每次只加一；"
            "现已理解慢启动从 cwnd=1 开始指数增长。",
            "known": ["慢启动从 cwnd=1 开始", "慢启动阶段指数增长"],
            "misconceptions": ["曾认为慢启动每次只把窗口加一"],
            "uncertain": ["何时切换到拥塞避免"],
            "explanation": "【S5-U】我原来以为慢启动是每次加一，其实是起点小。",
            "status": "PARTIAL",
        },
        "evidence": {
            "concept": "main",
            "observation": "【S5-E】用户把“慢启动”误解为线性增长，已纠正为指数增长。",
            "correctness": "PARTIAL",
        },
        "revisions": [
            {
                "no": 1,
                "summary": "【S5-R1】用户认为慢启动每次只把窗口加一。",
                "explanation": "【S5-R1】慢启动就是每次加一。",
            },
            {
                "no": 2,
                "summary": "【S5-R2】用户已纠正：慢启动从 1 开始指数增长。",
                "explanation": "【S5-R2】慢启动慢在起点小，不是每次只加一。",
            },
        ],
        "currentRevisionNo": 2,
        "memory": {
            "concept": "main",
            "content": "【S5-M】用户曾把 TCP 慢启动理解为线性加一，"
            "复盘时希望看到当时的原话和纠正过程。",
            "importance": 0.9,
        },
        "document": {
            "name": "s5-slow-start.md",
            "text": """# S5 TCP 慢启动

慢启动（slow start）并不是“增长得慢”，而是“从一个很小的拥塞窗口
开始”。初始 cwnd 通常为 1 个 MSS；每收到一个 ACK，cwnd 增加一个
MSS，因此每个 RTT 大约翻倍，呈指数增长。

## 为什么叫慢启动

名字来自“起点很小、需要时间爬升”，而不是增长速度慢。到达慢启动
阈值 ssthresh 后，TCP 进入拥塞避免，改为每个 RTT 线性增加一个 MSS。

## 常见误解

把慢启动理解成“每个 RTT 只加一”是常见误解；那更接近拥塞避免的
线性增长。判断一个说法是否正确，可以看它描述的是起点、增长曲线，
还是进入拥塞避免后的行为。
""",
        },
        "expect": {
            "C_intent": "LEARNING_REVIEW",
            "C_must_include": ["knowledge", "understanding", "memory", "trajectory", "evidence"],
            "C_must_exclude": [],
            "C_session": True,
            "B_session": True,
            "B_must_include": ["knowledge", "understanding", "memory", "trajectory"],
            "A_must_exclude": [
                "understanding",
                "memory",
                "evidence",
                "trajectory",
                "session",
                "history",
            ],
            "degraded_cognitive": False,
            "hard_answer_markers": ["【S5-HISTORY】"],
        },
        "notes": (
            "复盘意图要求 C 的 Manifest 同时含 revision 与 evidence；"
            "回答必须逐字引用含 【S5-HISTORY】 的原话，作为历史使用硬证据。"
        ),
    },
    {
        "id": "S6",
        "name": "当前掌握程度",
        "space": {
            "name": "S6 虚拟内存掌握度",
            "description": "询问当前掌握程度与薄弱点",
        },
        "concepts": [
            {
                "key": "main",
                "canonical": "虚拟内存",
                "display": "虚拟内存",
                "description": "虚拟内存为进程提供独立地址空间并通过页表映射",
                "difficulty": 3,
            }
        ],
        "sessions": [
            {
                "key": "s6",
                "title": "虚拟内存：地址转换与 TLB",
                "goal": "理解虚拟内存与页表",
                "focus": "TLB 与页表的分工",
            }
        ],
        "conversations": [
            {
                "key": "current",
                "title": "S6 当前掌握程度",
                "session": "s6",
                "history": [
                    [
                        "USER",
                        "【S6-HISTORY】虚拟地址到物理地址要经过页表，这个流程我画过。",
                    ],
                    [
                        "ASSISTANT",
                        "【S6-ASSIST】好，下一步可以看看 TLB miss 的代价。",
                    ],
                ],
            }
        ],
        "currentConversation": "current",
        "question": "我现在对虚拟内存掌握到什么程度？哪里还薄弱？",
        "understanding": {
            "concept": "main",
            "summary": "【S6-U】能解释虚拟地址到物理地址的转换，"
            "但说不清 TLB miss 后页表遍历的代价。",
            "known": ["虚拟地址转换流程", "页表的作用"],
            "misconceptions": [],
            "uncertain": ["TLB miss 的代价", "多级页表的访存次数"],
            "explanation": "【S6-U】虚拟内存让每个进程看到独立地址空间。",
            "status": "PARTIAL",
        },
        "evidence": {
            "concept": "main",
            "observation": "【S6-E】能讲清地址转换流程，未量化 TLB miss 代价。",
            "correctness": "PARTIAL",
        },
        "revisions": [
            {
                "no": 1,
                "summary": "【S6-R1】用户掌握了地址转换流程。",
                "explanation": "【S6-R1】虚拟地址经过页表映射到物理地址。",
            }
        ],
        "currentRevisionNo": 1,
        "memory": {
            "concept": "main",
            "content": "【S6-M】用户偏好先画地址转换流程图再记细节；"
            "在虚拟内存上已经会画流程，但对 TLB miss 代价还不确定。",
            "importance": 0.85,
        },
        "document": {
            "name": "s6-virtual-memory.md",
            "text": """# S6 虚拟内存

虚拟内存为每个进程提供独立的虚拟地址空间。CPU 发出的虚拟地址由
MMU 通过页表翻译成物理地址；页表通常有多级，每一级都要访问内存。

## TLB 的作用

TLB 缓存最近使用的页表项。TLB 命中时地址转换只需一次查找；
TLB miss 时才需要遍历页表，并把结果填回 TLB。因此评估一个人是否
真正掌握虚拟内存，可以看他能否说清 TLB 命中/miss 两条路径的代价。

## 多级页表

多级页表用树形结构减少常驻内存，但一次地址转换可能需要多次访存。
大页可以减少页表层级和 TLB 压力，这是性能优化中常见的取舍。
""",
        },
        "expect": {
            "C_intent": "META_QUESTION",
            "C_must_include": ["knowledge", "understanding", "memory", "evidence", "session"],
            "C_must_exclude": ["trajectory"],
            "C_session": True,
            "B_session": True,
            "B_must_include": ["knowledge", "understanding", "memory"],
            "A_must_exclude": [
                "understanding",
                "memory",
                "evidence",
                "trajectory",
                "session",
                "history",
            ],
            "degraded_cognitive": False,
        },
        "notes": "META_QUESTION：C 读取当前 Understanding + Evidence，不读 Revision。",
    },
    {
        "id": "S7",
        "name": "话题切换",
        "space": {
            "name": "S7 Redis 到 Raft",
            "description": "同一 Space 内从 Redis 话题切换到 Raft",
        },
        "concepts": [
            {
                "key": "old",
                "canonical": "Redis 持久化",
                "display": "Redis 持久化",
                "description": "Redis 的 RDB 与 AOF 持久化机制",
                "difficulty": 2,
            },
            {
                "key": "main",
                "canonical": "Raft 共识",
                "display": "Raft 共识",
                "description": "Raft 通过领导者选举和日志复制实现共识",
                "difficulty": 3,
            },
        ],
        "sessions": [
            {
                "key": "s7",
                "title": "Redis 持久化：AOF 重写",
                "goal": "理解 Redis 持久化与 AOF",
                "focus": "AOF 重写会不会阻塞主线程",
            }
        ],
        "conversations": [
            {
                "key": "current",
                "title": "S7 话题切换",
                "session": "s7",
                "history": [
                    [
                        "USER",
                        "【S7-HISTORY】AOF 重写会不会阻塞 Redis 主线程？",
                    ],
                    [
                        "ASSISTANT",
                        "【S7-ASSIST】重写由子进程完成，主线程只做增量同步。",
                    ],
                ],
            }
        ],
        "currentConversation": "current",
        "question": "Raft 共识的领导者选举是怎么实现的？",
        "understanding": {
            "concept": "old",
            "summary": "【S7-U-REDIS】用户理解了 AOF 重写由子进程完成。",
            "known": ["AOF 重写由子进程执行"],
            "misconceptions": [],
            "uncertain": ["重写期间的增量同步"],
            "explanation": "【S7-U-REDIS】AOF 重写不阻塞主线程。",
            "status": "PARTIAL",
        },
        "evidence": {
            "concept": "old",
            "observation": "【S7-E】用户已理解 AOF 重写的基本并发模型。",
            "correctness": "PARTIAL",
        },
        "revisions": [
            {
                "no": 1,
                "summary": "【S7-R1】用户理解了 AOF 重写由子进程完成。",
                "explanation": "【S7-R1】AOF 重写不阻塞主线程。",
            }
        ],
        "currentRevisionNo": 1,
        "memory": {
            "concept": "old",
            "content": "【S7-M】用户喜欢把 Redis 持久化和主线程阻塞一起考虑。",
            "importance": 0.7,
        },
        "document": {
            "name": "s7-raft-election.md",
            "text": """# S7 Raft 领导者选举

Raft 把节点分为 follower、candidate 和 leader。每个节点维护一个
随机的选举超时；follower 在超时内没有收到 leader 的心跳，就增加
当前任期并转为 candidate，向其他节点请求投票。

## 选举过程

candidate 先给自己投票，然后并行发送 RequestVote。每个节点在同一
任期内最多投一票，且只给日志至少与自己一样新的 candidate 投票。
获得多数票的 candidate 成为 leader，并定期发送心跳维持权威。

## 任期与安全性

任期号单调递增，用来识别过期消息。选举失败时随机化超时后重试，
避免所有节点同时竞选造成分裂投票。Raft 的选举安全性保证同一任期
最多一个 leader。
""",
        },
        "expect": {
            "C_intent": "LEARNING_EXPLANATION",
            "C_must_include": ["knowledge"],
            "C_must_exclude": ["session", "understanding", "memory"],
            "C_session": False,
            "B_session": True,
            "B_must_include": ["knowledge", "session", "history"],
            "A_must_exclude": [
                "understanding",
                "memory",
                "evidence",
                "trajectory",
                "session",
                "history",
            ],
            "degraded_cognitive": False,
        },
        "notes": (
            "当前问题命中 Raft，Session 文本只含 Redis，Policy 判定概念不匹配，"
            "C 不注入旧 Session；B 走 plan=None 仍注入旧 Session，是预期的近似缺陷。"
        ),
    },
    {
        "id": "S8",
        "name": "新旧知识冲突",
        "space": {
            "name": "S8 CAP 新旧知识",
            "description": "个人旧理解与文档新解释冲突",
        },
        "concepts": [
            {
                "key": "main",
                "canonical": "CAP 定理",
                "display": "CAP 定理",
                "description": "分布式系统中的一致性、可用性与分区容忍性",
                "difficulty": 3,
            }
        ],
        "sessions": [
            {
                "key": "s8",
                "title": "CAP 定理：三选二还是取舍",
                "goal": "理解 CAP 定理",
                "focus": "一致性与可用性的取舍",
            }
        ],
        "conversations": [
            {
                "key": "current",
                "title": "S8 新旧知识冲突",
                "session": "s8",
                "history": [
                    [
                        "USER",
                        "【S8-HISTORY】我记得 CAP 就是三选二，任何系统只能选两个。",
                    ],
                    [
                        "ASSISTANT",
                        "【S8-ASSIST】这是常见简化说法，分布式系统里 P 往往不可放弃。",
                    ],
                ],
            }
        ],
        "currentConversation": "current",
        "question": "CAP 定理是不是必须三选二？",
        "understanding": {
            "concept": "main",
            "summary": "【S8-U】用户原先认为 CAP 是三选二；"
            "现在仍在核对这个说法是否准确。",
            "known": ["知道 C/A/P 三个属性"],
            "misconceptions": ["认为 CAP 必须三选二"],
            "uncertain": ["P 是否可选", "CA 系统是否存在"],
            "explanation": "【S8-U】我以前认为 CAP 就是三选二。",
            "status": "PARTIAL",
        },
        "evidence": {
            "concept": "main",
            "observation": "【S8-E】用户对 CAP 的理解停留在“三选二”的口号。",
            "correctness": "PARTIAL",
        },
        "revisions": [
            {
                "no": 1,
                "summary": "【S8-R1】用户把 CAP 记成三选二。",
                "explanation": "【S8-R1】CAP 就是三选二。",
            }
        ],
        "currentRevisionNo": 1,
        "memory": {
            "concept": "main",
            "content": "【S8-M】用户用“三选二”记忆 CAP，"
            "需要把旧口诀和分布式系统的实际取舍区分开。",
            "importance": 0.8,
        },
        "document": {
            "name": "s8-cap.md",
            "text": """# S8 CAP 定理：不是简单的三选二

CAP 指一致性（Consistency）、可用性（Availability）和分区容忍性
（Partition tolerance）。它描述的是：当网络分区发生时，系统必须在
一致性和可用性之间做取舍。

## 为什么不是三选二

在分布式系统中，网络分区不是“可以选择放弃”的选项；只要系统跨网络
部署，就必须容忍分区。因此真正可选的通常是 CP 还是 AP，而不是在
C、A、P 中任选两个。所谓 CA 系统只适用于单机或不会发生分区的环境，
在真正的分布式部署里并不成立。

## 常见误解

“三选二”是方便记忆的口号，但它把 P 当成了可以放弃的选项，容易让人
误以为存在同时满足 C 和 A、又不需要处理分区的分布式系统。更准确的
说法是：分区发生时在 C 与 A 之间取舍。
""",
        },
        "expect": {
            "C_intent": "LEARNING_EXPLANATION",
            "C_must_include": ["knowledge", "understanding", "memory"],
            "C_must_exclude": [],
            "C_session": True,
            "B_session": True,
            "B_must_include": ["knowledge", "understanding", "memory", "history"],
            "A_must_exclude": [
                "understanding",
                "memory",
                "evidence",
                "trajectory",
                "session",
                "history",
            ],
            "degraded_cognitive": False,
        },
        "notes": "个人记忆中的旧口诀与文档新解释冲突；检查回答是否采用文档口径。",
    },
    {
        "id": "S9",
        "name": "检索异常（DEGRADED）",
        "space": {
            "name": "S9 检索异常",
            "description": "Understanding 检索失败，验证 B 折叠 EMPTY、C 保留 DEGRADED",
        },
        "concepts": [
            {
                "key": "main",
                "canonical": "进程调度",
                "display": "进程调度",
                "description": "操作系统选择下一个运行进程的策略",
                "difficulty": 2,
            }
        ],
        "sessions": [
            {
                "key": "s9",
                "title": "进程调度：算法与指标",
                "goal": "理解常见进程调度算法",
                "focus": "时间片与优先级的取舍",
            }
        ],
        "conversations": [
            {
                "key": "current",
                "title": "S9 检索异常",
                "session": "s9",
                "history": [
                    [
                        "USER",
                        "【S9-HISTORY】进程调度是不是只要按优先级排队就行？",
                    ],
                    [
                        "ASSISTANT",
                        "【S9-ASSIST】优先级只是其中一种策略，还要考虑饥饿和时间片。",
                    ],
                ],
            }
        ],
        "currentConversation": "current",
        "question": "进程调度有哪些常见算法？",
        "understanding": {
            "concept": "main",
            "summary": "【S9-U】用户知道优先级调度，但还不清楚轮转与多级反馈队列。",
            "known": ["优先级调度"],
            "misconceptions": [],
            "uncertain": ["时间片轮转", "多级反馈队列"],
            "explanation": "【S9-U】调度就是按优先级选进程。",
            # 刻意写入非法枚举值：Spring 读取该行时失败，Agent 将
            # Understanding 标记为 DEGRADED。这是本场景唯一的人为故障。
            "status": "CORRUPT",
        },
        "evidence": {
            "concept": "main",
            "observation": "【S9-E】用户尚未区分轮转与优先级调度的适用场景。",
            "correctness": "PARTIAL",
        },
        "revisions": [
            {
                "no": 1,
                "summary": "【S9-R1】用户知道优先级调度。",
                "explanation": "【S9-R1】调度就是按优先级选进程。",
            }
        ],
        "currentRevisionNo": 1,
        "memory": {
            "concept": "main",
            "content": "【S9-M】用户偏好用时间片和优先级理解进程调度。",
            "importance": 0.8,
        },
        "document": {
            "name": "s9-scheduling.md",
            "text": """# S9 进程调度算法

常见调度算法包括先来先服务（FCFS）、最短作业优先（SJF）、时间片
轮转（RR）、优先级调度和多级反馈队列（MLFQ）。

## 各自的特点

FCFS 简单但平均等待时间可能很长；SJF 平均等待时间短，但需要知道
运行时间，且长作业可能饥饿；RR 用固定时间片保证响应性，时间片过小
会增加上下文切换；优先级调度需要处理饥饿，常用老化解决；MLFQ 用
多个优先级队列和动态反馈兼顾响应性与吞吐。

## 评价指标

常用指标包括 CPU 利用率、吞吐量、周转时间、等待时间和响应时间。
没有一种算法在所有指标上都最优，选择取决于工作负载和交互性要求。
""",
        },
        "expect": {
            "C_intent": "LEARNING_EXPLANATION",
            "C_must_include": ["knowledge", "memory", "cognitive_status"],
            "C_must_exclude": ["trajectory"],
            "C_session": True,
            "B_session": True,
            "B_must_include": ["knowledge", "memory"],
            "A_must_exclude": [
                "understanding",
                "memory",
                "evidence",
                "trajectory",
                "session",
                "history",
            ],
            "degraded_cognitive": True,
            "degraded_source": "understanding-status:DEGRADED",
        },
        "notes": (
            "Understanding 行使用非法 status。C 的 Manifest 应出现 "
            "cognitive_status 与 understanding-status:DEGRADED；B 把该状态折叠为 "
            "EMPTY，因此不应出现同一 source。此场景同时验证 B 的近似的已知缺陷。"
        ),
    },
    {
        "id": "S10",
        "name": "上下文预算压力",
        "space": {
            "name": "S10 预算压力",
            "description": "长历史、长 Understanding/Memory 触发预算截断",
        },
        "concepts": [
            {
                "key": "main",
                "canonical": "文件系统",
                "display": "文件系统",
                "description": "文件系统的组织、缓存与日志机制",
                "difficulty": 3,
            }
        ],
        "sessions": [
            {
                "key": "s10",
                "title": "文件系统：日志与一致性",
                "goal": "理解文件系统日志（journaling）如何保证一致性",
                "focus": "日志提交与检查点的顺序",
            }
        ],
        "conversations": [
            {
                "key": "current",
                "title": "S10 上下文预算压力",
                "session": "s10",
                "history": _s10_history(),
            }
        ],
        "currentConversation": "current",
        "question": "文件系统的日志（journaling）为什么能保证一致性？",
        "understanding": {
            "concept": "main",
            "summary": (
                "【S10-U】用户对文件系统的理解分散在 inode、目录项、页缓存、"
                "日志提交和检查点几个部分，能说清单个概念，但还没有把它们"
                "串成一条崩溃恢复链路。"
            ),
            "known": _s10_known(),
            "misconceptions": [],
            "uncertain": _s10_uncertain(),
            "explanation": (
                "【S10-U】日志先把元数据变更写进日志区，提交后再更新真正的"
                "元数据位置；崩溃后可以重放日志。"
            ),
            "status": "PARTIAL",
        },
        "evidence": {
            "concept": "main",
            "observation": (
                "【S10-E】用户能复述日志先写后提交的步骤，但没有解释为什么"
                "提交顺序能避免部分更新。"
            ),
            "correctness": "PARTIAL",
        },
        "revisions": [
            {
                "no": 1,
                "summary": "【S10-R1】用户已读过 inode 与目录项的基本结构。",
                "explanation": "【S10-R1】inode 保存元数据，目录项保存名字到 inode 的映射。",
            }
        ],
        "currentRevisionNo": 1,
        "memory": {
            "concept": "main",
            "content": (
                "【S10-M】用户偏好从“崩溃后会发生什么”倒推文件系统设计，"
                "习惯把日志、检查点和写回顺序画成时间线；在学习文件系统时"
                "希望先看到完整链路再看局部优化。"
            ),
            "importance": 0.9,
        },
        "document": {
            "name": "s10-journaling.md",
            "text": """# S10 文件系统日志（journaling）

文件系统的一次元数据更新往往涉及多个磁盘块。如果崩溃发生在更新
中途，磁盘上可能出现“一半新、一半旧”的状态，破坏文件系统一致性。

## 日志的基本流程

日志式文件系统先把要修改的元数据块写入日志区，并在日志记录中写入
事务边界；只有日志提交（commit）成功后，才把日志内容写回真正的
元数据位置（checkpoint）。崩溃恢复时，如果日志已提交，就重放日志；
如果未提交，就丢弃这条事务。

## 为什么能保证一致性

关键在于“先写日志、后写数据”的顺序，以及日志记录带有事务边界。
恢复程序只需检查日志是否完整提交，就能决定重放还是丢弃，而不会
看到部分更新的元数据。

## 写屏障与顺序

日志提交前必须保证日志数据真正落盘，因此需要写屏障（write barrier）
或等价机制。没有顺序保证时，即使代码先写日志，设备也可能重排写入，
恢复时就无法判断事务是否完整。
""",
        },
        "expect": {
            "C_intent": "LEARNING_EXPLANATION",
            "C_must_include": ["knowledge", "understanding", "memory", "session", "history"],
            "C_must_exclude": [],
            "C_session": True,
            "B_session": True,
            "B_must_include": ["knowledge", "understanding", "memory", "history"],
            "A_must_exclude": [
                "understanding",
                "memory",
                "evidence",
                "trajectory",
                "session",
                "history",
            ],
            "degraded_cognitive": False,
            "expect_truncation_or_drop": True,
        },
        "notes": (
            "14 条长历史 + 长 Understanding/Memory，预期 Manifest 出现 "
            "TRUNCATED/OMITTED 或 budget.used 接近 effective；用于验证预算压力。"
        ),
    },
]

SCENARIO_BY_ID = {item["id"]: item for item in SCENARIOS}


def scenario_summary() -> list[dict[str, Any]]:
    """报告用的场景摘要（不含文档正文与完整上下文正文）。"""
    rows: list[dict[str, Any]] = []
    for item in SCENARIOS:
        rows.append(
            {
                "id": item["id"],
                "name": item["name"],
                "space": item["space"]["name"],
                "concepts": [concept["canonical"] for concept in item["concepts"]],
                "sessions": [session["title"] for session in item["sessions"]],
                "conversations": [conv["title"] for conv in item["conversations"]],
                "question": item["question"],
                "expectedCIntent": item["expect"].get("C_intent"),
                "C_must_include": item["expect"].get("C_must_include", []),
                "A_must_exclude": item["expect"].get("A_must_exclude", []),
                "notes": item.get("notes", ""),
            }
        )
    return rows
