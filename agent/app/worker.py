"""`python -m app.worker` 的入口。

单独放在这里，而不是让 FastAPI 启动时顺便起一个消费者：
HTTP 服务与 MQ Worker 可以各自扩容、各自重启。只想跑聊天时不需要 RabbitMQ。
"""

from app.messaging.worker import main

if __name__ == "__main__":
    main()
