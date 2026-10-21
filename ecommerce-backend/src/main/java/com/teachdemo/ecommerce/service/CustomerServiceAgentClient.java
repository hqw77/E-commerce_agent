package com.teachdemo.ecommerce.service;

import com.fasterxml.jackson.databind.JsonNode;
import com.teachdemo.ecommerce.dto.AgentApprovalDecisionRequest;
import com.teachdemo.ecommerce.dto.AgentChatRequest;
import com.teachdemo.ecommerce.dto.AgentConsentRequest;
import com.teachdemo.ecommerce.dto.AgentPendingApproval;
import com.teachdemo.ecommerce.dto.AgentResumeRequest;
import com.teachdemo.ecommerce.dto.AgentWorkflowStatus;
import java.util.List;

public interface CustomerServiceAgentClient {

    JsonNode chat(AgentChatRequest request);

    JsonNode consent(AgentConsentRequest request);

    JsonNode resume(AgentResumeRequest request);

    List<AgentPendingApproval> listPendingApprovals(String adminUserId);

    JsonNode decideApproval(String adminUserId, String workflowId, AgentApprovalDecisionRequest request);

    AgentWorkflowStatus getWorkflowStatus(String userId, String workflowId);
}
