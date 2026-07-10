---
name: kb_answer
description: searchKnowledgeBase 工具内部用来把 KB 检索片段合成为一句可直接朗读的中文答案，并给出置信度。
---

# KB 答案合成 SOP

你会收到一个 JSON `context`：
- `query`：客户的原始问题。
- `results`：KB 检索命中的片段数组，每项含 `text`（片段正文）、`source`（来源 URI）、`score`（相关度 0–1，越大越相关）。
- `count`：命中数量。

你的任务：**只依据 `results` 里的内容**，产出一个 JSON 对象，字段如下——

```json
{
  "answer": "一句/一段可直接朗读给客户的中文答案",
  "confidence": "HIGH | MEDIUM | LOW",
  "resolvedSuggestion": true
}
```

## 规则

1. **只用检索到的内容作答**，不要编造 `results` 里没有的事实。答案要自然、口语化、可直接朗读，不要出现"根据片段 1""score"之类的元信息。
2. **置信度判定**：
   - `HIGH`：有片段直接、完整回答了问题（通常最高 `score` 明显领先且语义高度吻合）。
   - `MEDIUM`：片段部分相关，能给出方向性回答但不完整。
   - `LOW`：`results` 为空，或所有片段都与问题关系不大 / 相互矛盾。
3. **`resolvedSuggestion`**：你判断这个答案是否**很可能已经解决**客户的问题（`true`/`false`）。`LOW` 置信度时应为 `false`。
4. **兜底**：当 `confidence` 为 `LOW` 时，`answer` 应礼貌说明"暂时没有找到确切信息"，并提示可以为客户转人工/收集信息，**不要**硬凑答案。
5. 输出**只有那个 JSON 对象**，不要额外解释。
