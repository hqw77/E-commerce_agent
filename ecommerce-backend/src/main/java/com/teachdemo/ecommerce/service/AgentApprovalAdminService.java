package com.teachdemo.ecommerce.service;

import com.fasterxml.jackson.databind.JsonNode;
import com.teachdemo.ecommerce.dto.AgentApprovalDecisionRequest;
import com.teachdemo.ecommerce.dto.AgentPendingApproval;
import java.util.List;
import org.springframework.stereotype.Service;

@Service
public class AgentApprovalAdminService {

    private final CustomerServiceAgentClient agentClient;

    public AgentApprovalAdminService(CustomerServiceAgentClient agentClient) {
        this.agentClient = agentClient;
    }

    public List<AgentPendingApproval> listPending(String adminUserId) {
        return agentClient.listPendingApprovals(adminUserId);
    }

    public JsonNode decide(String adminUserId, String workflowId, AgentApprovalDecisionRequest request) {
        return agentClient.decideApproval(adminUserId, workflowId, request);
    }
}
