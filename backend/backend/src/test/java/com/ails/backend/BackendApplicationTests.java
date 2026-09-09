package com.ails.backend;

import org.junit.jupiter.api.Disabled;
import org.junit.jupiter.api.Test;
import org.springframework.boot.test.context.SpringBootTest;

/**
 * 上下文加载冒烟测试：需要本地 MySQL（ails 库 + ails.jwt.secret 配置）可连通。
 * 未配置数据库时请用 -DskipTests 或 --tests 排除；本地跑 mvn test 前请先启动 MySQL。
 */
@Disabled("需要本地 MySQL 与库表已初始化，CI/无库环境下跳过")
@SpringBootTest
class BackendApplicationTests {

    @Test
    void contextLoads() {
    }

}
