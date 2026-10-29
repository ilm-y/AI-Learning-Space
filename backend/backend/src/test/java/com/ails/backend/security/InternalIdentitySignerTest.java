package com.ails.backend.security;

import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;

import java.time.Clock;
import java.time.Instant;
import java.time.ZoneOffset;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

class InternalIdentitySignerTest {

    private static final String IDENTITY_SECRET = "unit-test-identity-secret";
    private static final String DELEGATION_SECRET = "unit-test-delegation-secret";

    private static InternalIdentitySigner signer(String identitySecret, String delegationSecret) {
        return signer(identitySecret, delegationSecret, Instant.parse("2026-01-01T00:00:00Z"));
    }

    private static InternalIdentitySigner signer(String identitySecret, String delegationSecret,
                                                 Instant now) {
        return new InternalIdentitySigner(
                identitySecret, delegationSecret, "learning-agent", 60,
                "learning-backend", 300, 5, 900, 3600,
                Clock.fixed(now, ZoneOffset.UTC));
    }

    @Test
    @DisplayName("Agent 声明绑定 sub/aud/issuer，且不能被后端委托密钥验证")
    void agentTokenRoundTripBindsSubjectAudienceAndIssuer() {
        InternalIdentitySigner signer = signer(IDENTITY_SECRET, DELEGATION_SECRET);
        String token = signer.sign(42L);

        assertThat(token).startsWith("v1.");
        assertThat(signer.verifyForAudience(token, "learning-agent")).contains(42L);
        assertThat(signer.verifyForAudience(token, "learning-backend")).isEmpty();
        assertThat(signer.verifyForBackend(token)).isEmpty();
        assertThat(signer("x" + IDENTITY_SECRET, DELEGATION_SECRET)
                .verifyForAudience(token, "learning-agent")).isEmpty();
    }

    @Test
    @DisplayName("委托声明绑定 scope，且只能由 delegation 密钥验证")
    void delegationTokenBindsScopeAndUsesSeparateKey() {
        InternalIdentitySigner signer = signer(IDENTITY_SECRET, DELEGATION_SECRET);
        String token = signer.signDelegation(7L);

        assertThat(signer.verifyDelegation(token))
                .hasValueSatisfying(claims -> {
                    assertThat(claims.userId()).isEqualTo(7L);
                    assertThat(claims.scope()).isEqualTo(InternalIdentitySigner.DELEGATION_SCOPE);
                });
        assertThat(signer.verifyForBackend(token)).contains(7L);
        // 用 Agent 持有的 identity 密钥签发的 learning-backend 声明不被接受：
        // 这正是修正前 Agent 能伪造后端身份的路径。
        String agentSigned = signer.signForAudience(7L, "learning-backend");
        assertThat(signer.verifyForBackend(agentSigned)).isEmpty();
        assertThat(signer.verifyForAudience(token, "learning-agent")).isEmpty();
    }

    @Test
    @DisplayName("篡改 payload 或签名都会被拒绝")
    void tamperedTokenIsRejected() {
        InternalIdentitySigner signer = signer(IDENTITY_SECRET, DELEGATION_SECRET);
        String token = signer.sign(42L);
        String[] parts = token.split("\\.");
        String tamperedPayload = parts[0] + "." + parts[1].substring(0, parts[1].length() - 1)
                + (parts[1].endsWith("A") ? "B" : "A") + "." + parts[2];

        assertThat(signer.verifyForAudience(tamperedPayload, "learning-agent")).isEmpty();
        assertThat(signer.verifyForAudience(token.substring(0, token.length() - 1) + "x",
                "learning-agent")).isEmpty();
        assertThat(signer.verifyForAudience("not-a-token", "learning-agent")).isEmpty();
        assertThat(signer.verifyForAudience(null, "learning-agent")).isEmpty();
    }

    @Test
    @DisplayName("未配置密钥时 fail closed：不能签发，也不能验证")
    void unconfiguredSignerFailsClosed() {
        InternalIdentitySigner signer = signer("", "");

        assertThatThrownBy(() -> signer.sign(1L))
                .isInstanceOf(IllegalStateException.class)
                .hasMessageContaining("identity-secret");
        assertThatThrownBy(() -> signer.signDelegation(1L))
                .isInstanceOf(IllegalStateException.class)
                .hasMessageContaining("delegation-secret");
        assertThat(signer.verifyForAudience("v1.abc.def", "learning-agent")).isEmpty();
        assertThat(signer.verifyForBackend("v1.abc.def")).isEmpty();
    }

    @Test
    @DisplayName("错误 audience、issuer、scope、有效期或 TTL 上界的委托声明都被拒绝")
    void invalidDelegationClaimsAreRejected() {
        InternalIdentitySigner signer = signer(IDENTITY_SECRET, DELEGATION_SECRET);

        assertThat(signer.verifyForBackend(signer.signDelegationForTest(
                7L, "learning-backend", InternalIdentitySigner.ISSUER,
                "analysis:write", 900))).isEmpty();
        assertThat(signer.verifyForBackend(signer.signDelegationForTest(
                7L, "learning-backend", "attacker-issuer",
                InternalIdentitySigner.DELEGATION_SCOPE, 900))).isEmpty();
        assertThat(signer.verifyForBackend(signer.signDelegationForTest(
                7L, "learning-agent", InternalIdentitySigner.ISSUER,
                InternalIdentitySigner.DELEGATION_SCOPE, 900))).isEmpty();
        // 超过 delegation 的最大有效期（3600 秒）
        assertThat(signer.verifyForBackend(signer.signDelegationForTest(
                7L, "learning-backend", InternalIdentitySigner.ISSUER,
                InternalIdentitySigner.DELEGATION_SCOPE, 7200))).isEmpty();
    }

    @Test
    @DisplayName("过期或签发时间在未来的声明被拒绝")
    void expiredAndFutureTokensAreRejected() {
        Instant issuedAt = Instant.parse("2026-01-01T00:00:00Z");
        String token = signer(IDENTITY_SECRET, DELEGATION_SECRET, issuedAt).signDelegation(9L);

        InternalIdentitySigner later = signer(
                IDENTITY_SECRET, DELEGATION_SECRET, issuedAt.plusSeconds(1000));
        assertThat(later.verifyForBackend(token)).isEmpty();

        InternalIdentitySigner earlier = signer(
                IDENTITY_SECRET, DELEGATION_SECRET, issuedAt.minusSeconds(100));
        assertThat(earlier.verifyForBackend(token)).isEmpty();
    }
}
