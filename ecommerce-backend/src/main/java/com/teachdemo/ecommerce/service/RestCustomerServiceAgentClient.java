package com.teachdemo.ecommerce.service;

import com.fasterxml.jackson.databind.JsonNode;
import com.teachdemo.ecommerce.dto.AgentApprovalDecisionRequest;
import com.teachdemo.ecommerce.dto.AgentChatRequest;
import com.teachdemo.ecommerce.dto.AgentConsentRequest;
import com.teachdemo.ecommerce.dto.AgentPendingApproval;
import com.teachdemo.ecommerce.dto.AgentResumeRequest;
import com.teachdemo.ecommerce.dto.AgentWorkflowStatus;
import com.teachdemo.ecommerce.security.AgentRequestSigner;
import java.util.List;
import java.util.Map;
import org.springframework.core.ParameterizedTypeReference;
import org.springframework.stereotype.Service;
import org.springframework.web.client.RestClient;

@Service
public class RestCustomerServiceAgentClient implements CustomerServiceAgentClient {

    private final RestClient customerServiceAgentRestClient;

    private final AgentRequestSigner signer;

    public RestCustomerServiceAgentClient(RestClient customerServiceAgentRestClient, AgentRequestSigner signer) {
        this.customerServiceAgentRestClient = customerServiceAgentRestClient;
        this.signer = signer;
    }

    @Override
    public JsonNode chat(AgentChatRequest request) {
        return post("/chat", request, signer.sign(request.getRuntimeUserId(), "user", "/chat"));
    }

    @Override
    public JsonNode consent(AgentConsentRequest request) {
        return post("/chat/consent", request, signer.sign(request.getUserId(), "user", "/chat/consent"));
    }

    @Override
    public JsonNode resume(AgentResumeRequest request) {
        return post(
            "/chat/resume",
            request,
            signer.sign(request.getReviewerId(), request.getReviewerRole(), "/chat/resume")
        );
    }

    @Override
    public List<AgentPendingApproval> listPendingApprovals(String adminUserId) {
        var spec = customerServiceAgentRestClient.get().uri("/admin/approvals");
        Map<String, String> headers = signer.sign(adminUserId, "admin", "/admin/approvals");
        if (!headers.isEmpty()) {
            spec = spec.headers(httpHeaders -> httpHeaders.setAll(headers));
        }
        return spec.retrieve().body(new ParameterizedTypeReference<List<AgentPendingApproval>>() {});
    }

    @Override
    public JsonNode decideApproval(String adminUserId, String workflowId, AgentApprovalDecisionRequest request) {
        String path = "/admin/approvals/" + workflowId;
        return post(path, request, signer.sign(adminUserId, "admin", path));
    }

    @Override
    public AgentWorkflowStatus getWorkflowStatus(String userId, String workflowId) {
        String path = "/chat/workflow/" + workflowId;
        var spec = customerServiceAgentRestClient.get().uri(path);
        Map<String, String> headers = signer.sign(userId, "user", path);
        if (!headers.isEmpty()) {
            spec = spec.headers(httpHeaders -> httpHeaders.setAll(headers));
        }
        return spec.retrieve().body(AgentWorkflowStatus.class);
    }

    private JsonNode post(String path, Object body, Map<String, String> headers) {
        RestClient.RequestBodySpec spec = customerServiceAgentRestClient.post().uri(path);
        if (!headers.isEmpty()) {
            spec = spec.headers(httpHeaders -> httpHeaders.setAll(headers));
        }
        return spec.body(body).retrieve().body(JsonNode.class);
    }
}
