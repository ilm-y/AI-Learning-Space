package com.ails.backend.config;

import com.ails.backend.security.InternalServiceAuthenticationFilter;
import com.ails.backend.security.JwtAuthenticationFilter;
import com.ails.backend.security.RestAccessDeniedHandler;
import com.ails.backend.security.RestAuthenticationEntryPoint;
import lombok.RequiredArgsConstructor;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.http.HttpMethod;
import org.springframework.security.config.Customizer;
import org.springframework.security.config.annotation.web.builders.HttpSecurity;
import org.springframework.security.config.annotation.web.configuration.EnableWebSecurity;
import org.springframework.security.config.http.SessionCreationPolicy;
import org.springframework.security.crypto.bcrypt.BCryptPasswordEncoder;
import org.springframework.security.crypto.password.PasswordEncoder;
import org.springframework.security.web.SecurityFilterChain;
import org.springframework.security.web.authentication.UsernamePasswordAuthenticationFilter;
import org.springframework.web.cors.CorsConfiguration;
import org.springframework.web.cors.CorsConfigurationSource;
import org.springframework.web.cors.UrlBasedCorsConfigurationSource;

import java.util.List;

/**
 * Spring Security 无状态 JWT 配置：
 * /api/auth/** 与 OPTIONS 放行，其余接口需携带 Authorization: Bearer token。
 */
@Configuration
@EnableWebSecurity
@RequiredArgsConstructor
public class SecurityConfig {

    private final JwtAuthenticationFilter jwtAuthenticationFilter;
    private final InternalServiceAuthenticationFilter internalServiceAuthenticationFilter;
    private final RestAuthenticationEntryPoint unauthorizedHandler;
    private final RestAccessDeniedHandler accessDeniedHandler;

    @Bean
    public PasswordEncoder passwordEncoder() {
        return new BCryptPasswordEncoder();
    }

    @Bean
    public SecurityFilterChain securityFilterChain(HttpSecurity http) throws Exception {
        http.csrf(csrf -> csrf.disable())
                .cors(Customizer.withDefaults())
                .sessionManagement(session -> session.sessionCreationPolicy(SessionCreationPolicy.STATELESS))
                .authorizeHttpRequests(auth -> auth
                        .requestMatchers(HttpMethod.OPTIONS, "/**").permitAll()
                        .requestMatchers("/api/auth/**").permitAll()
                        .anyRequest().authenticated())
                .exceptionHandling(ex -> ex
                        .authenticationEntryPoint(unauthorizedHandler)
                        .accessDeniedHandler(accessDeniedHandler))
                // 顺序：内部服务凭据先建立「代用户」身份，再由 JWT 过滤器处理普通用户请求。
                // 两个过滤器都只在 SecurityContext 为空时写入，先成功的一方生效。
                .addFilterBefore(internalServiceAuthenticationFilter, JwtAuthenticationFilter.class)
                .addFilterBefore(jwtAuthenticationFilter, UsernamePasswordAuthenticationFilter.class);
        return http.build();
    }

    /**
     * 允许本地 Flutter Web/桌面调试跨域。
     * <p>
     * <b>刻意不开 {@code allowCredentials}。</b>本项目是无状态 JWT，凭证走
     * {@code Authorization} 请求头，浏览器<b>不会</b>自动附带 —— 也就是说这个开关打开之后
     * 一个请求都不会多带什么东西，只是把「任意站点可以用用户 Cookie 调这些接口」这条路敞开。
     * 前端 Flutter Web 需要读响应头时用 {@code exposedHeaders} 就够，它与凭证无关。
     * <p>
     * 如果哪天改成 Cookie 认证，正确的做法也不是把这里改回 true，而是把
     * {@code allowedOriginPatterns} 收成一份明确的域名白名单 —— 通配来源 + 允许凭证
     * 是「任何网站都能以登录用户身份调你的接口」的同义词。
     */
    @Bean
    public CorsConfigurationSource corsConfigurationSource() {
        CorsConfiguration config = new CorsConfiguration();
        config.setAllowedOriginPatterns(List.of("*"));
        config.setAllowedMethods(List.of("GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"));
        config.setAllowedHeaders(List.of("*"));
        config.setExposedHeaders(List.of("Authorization"));
        config.setAllowCredentials(false);
        config.setMaxAge(3600L);

        UrlBasedCorsConfigurationSource source = new UrlBasedCorsConfigurationSource();
        source.registerCorsConfiguration("/**", config);
        return source;
    }
}
