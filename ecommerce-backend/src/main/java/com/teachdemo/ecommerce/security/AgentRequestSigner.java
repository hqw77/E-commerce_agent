package com.teachdemo.ecommerce.security;

import com.teachdemo.ecommerce.config.CustomerServiceAgentProperties;
import java.nio.charset.StandardCharsets;
import java.security.InvalidKeyException;
import java.security.NoSuchAlgorithmException;
import java.time.Instant;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.UUID;
import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;
import org.springframework.stereotype.Component;
import org.springframework.util.StringUtils;

@Component
public class AgentRequestSigner {

    private final CustomerServiceAgentProperties properties;

    public AgentRequestSigner(CustomerServiceAgentProperties properties) {
        this.properties = properties;
        if (isHmac() && !StringUtils.hasText(properties.getInternalSecret())) {
            throw new IllegalStateException("agent.service.internal-secret 不能为空");
        }
    }

    public boolean isHmac() {
        return "hmac".equalsIgnoreCase(properties.getAuthMode());
    }

    public Map<String, String> sign(String userId, String role, String path) {
        if (!isHmac()) {
            return Map.of();
        }
        if (!StringUtils.hasText(userId)) {
            throw new IllegalArgumentException("签名需要 userId");
        }
        String timestamp = String.valueOf(Instant.now().getEpochSecond());
        String nonce = UUID.randomUUID().toString().replace("-", "");
        String payload = userId + "|" + role + "|" + timestamp + "|" + nonce + "|" + path;
        String signature = hmacHex(properties.getInternalSecret(), payload);

        Map<String, String> headers = new LinkedHashMap<>();
        headers.put("X-Agent-User-Id", userId);
        headers.put("X-Agent-Role", role);
        headers.put("X-Agent-Timestamp", timestamp);
        headers.put("X-Agent-Nonce", nonce);
        headers.put("X-Agent-Signature", signature);
        return headers;
    }

    private static String hmacHex(String secret, String payload) {
        try {
            Mac mac = Mac.getInstance("HmacSHA256");
            mac.init(new SecretKeySpec(secret.getBytes(StandardCharsets.UTF_8), "HmacSHA256"));
            return HexFormat.of().formatHex(mac.doFinal(payload.getBytes(StandardCharsets.UTF_8)));
        } catch (NoSuchAlgorithmException | InvalidKeyException ex) {
            throw new IllegalStateException("无法生成 Agent 请求签名", ex);
        }
    }
}
