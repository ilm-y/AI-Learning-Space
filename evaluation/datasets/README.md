# 评测数据集

当前离线用例直接复用 `agent/fixtures/demo_space.json` —— 它与 `agent/tests/`
用的是同一份 TLB 场景，好处是「单测看到的输入」和「评测看到的输入」不会漂移。

`runner` 解析 fixture 的顺序：

1. `agent/fixtures/<name>`
2. `evaluation/datasets/<name>`

需要新的场景（例如多空间、多用户、已被纠正过的误区）时，在这里新增 JSON 文件，
并在用例里写 `"fixture": "<name>"`。字段形状必须与后端响应一致 ——
Fixture Retriever 用的是同一套 Pydantic 模型，形状不对会在加载时直接报错。

live 用例不读这里的文件：它通过 HTTP 在真实数据库里建空间 / 概念，
种子数据就是 `cases/live/*.json` 里的 `setup`。
