package com.ails.backend.security;

import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import static org.assertj.core.api.Assertions.assertThatCode;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * 内部凭据配置的启动行为：错误配置必须 fail closed。
 *
 * <p>这些测试不启动 Spring 上下文，直接调用 {@code @PostConstruct} 方法，
 * 因此不依赖 MySQL，可以在默认 {@code mvn test} 中执行。</p>
 */
class InternalSecurityConfigValidatorTest {

    private static final String TOKEN = "it-token";
    private static final String IDENTITY = "it-identity-secret";
    private static final String DELEGATION = "it-delegation-secret";

    private static InternalSecurityConfigValidator validator(String token, String identity,
                                                             String delegation) {
        return new InternalSecurityConfigValidator(
                token, identity, delegation, "learning-agent", "learning-backend");
    }

    @Test
    @DisplayName("内部入口未启用时不校验密钥，应用可只跑普通 JWT 链路")
    void disabledInternalEntryDoesNotRequireSecrets() {
        assertThatCode(() -> validator("", "", "").validate()).doesNotThrowAnyException();
        assertThatCode(() -> validator(" ", "", "").validate()).doesNotThrowAnyException();
    }

    @Test
    @DisplayName("启用内部入口但任一签名密钥缺失时拒绝启动")
    void enabledInternalEntryRequiresBothSecrets() {
        assertThatThrownBy(() -> validator(TOKEN, "", DELEGATION).validate())
                .isInstanceOf(IllegalStateException.class)
                .hasMessageContaining("identity-secret")
                .hasMessageContaining("delegation-secret");

        assertThatThrownBy(() -> validator(TOKEN, IDENTITY, "").validate())
                .isInstanceOf(IllegalStateException.class)
                .hasMessageContaining("identity-secret")
                .hasMessageContaining("delegation-secret");
    }

    @Test
    @DisplayName("两把签名密钥相同、或与共享 token 相同时拒绝启动")
    void equalSecretsAreRejected() {
        assertThatThrownBy(() -> validator(TOKEN, IDENTITY, IDENTITY).validate())
                .isInstanceOf(IllegalStateException.class)
                .hasMessageContaining("不能相同");

        assertThatThrownBy(() -> validator(IDENTITY, IDENTITY, DELEGATION).validate())
                .isInstanceOf(IllegalStateException.class)
                .hasMessageContaining("token");

        assertThatThrownBy(() -> validator(DELEGATION, IDENTITY, DELEGATION).validate())
                .isInstanceOf(IllegalStateException.class)
                .hasMessageContaining("token");
    }

    @Test
    @DisplayName("两个 audience 相同意味着 Agent 侧声明可被后端接受，拒绝启动")
    void equalAudiencesAreRejected() {
        InternalSecurityConfigValidator sameAudience = new InternalSecurityConfigValidator(
                TOKEN, IDENTITY, DELEGATION, "learning-backend", "learning-backend");
        assertThatThrownBy(sameAudience::validate)
                .isInstanceOf(IllegalStateException.class)
                .hasMessageContaining("audience");
    }

    @Test
    @DisplayName("完整且互不相同的配置允许启动")
    void validConfigurationStarts() {
        assertThatCode(() -> validator(TOKEN, IDENTITY, DELEGATION).validate())
                .doesNotThrowAnyException();
    }
}
