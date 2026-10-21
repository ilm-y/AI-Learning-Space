"""异步学习分析：从一轮对话里抽出证据、理解提案、记忆候选与验证决策。

位置在 Tutor 之后 —— 回答已经推给用户了，这一步不阻塞对话：

```text
User Message + Assistant Reply
        ↓
Concept Detection → 读当前 Understanding / Memory / Evidence
        ↓
Analyzer（LLM 结构化输出，Mock 时用确定性启发式）
        ↓
EvidenceCandidate / UnderstandingProposal / MemoryCandidate / VerificationDecision
        ↓
POST /agent/analyze → 后端按固定顺序写入
```

Agent 只生产**候选**，不写数据库。去重、误区生命周期、作用域可见性、
事务顺序这些「写入语义」都在后端；本模块一旦开始自己判断这些，
就会出现两套真相。
"""

from app.analysis.analyzer import ANALYZER_VERSION, AnalysisError, LearningAnalyzer

__all__ = ["ANALYZER_VERSION", "AnalysisError", "LearningAnalyzer"]
