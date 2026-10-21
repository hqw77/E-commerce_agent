package com.teachdemo.ecommerce.controller;

import com.fasterxml.jackson.databind.JsonNode;
import com.teachdemo.ecommerce.dto.AdminAgentApprovalDecisionRequest;
import com.teachdemo.ecommerce.dto.AgentApprovalDecisionRequest;
import com.teachdemo.ecommerce.dto.AgentPendingApproval;
import com.teachdemo.ecommerce.dto.ApiResponse;
import com.teachdemo.ecommerce.security.AccountPrincipal;
import com.teachdemo.ecommerce.service.AgentApprovalAdminService;
import io.swagger.v3.oas.annotations.Operation;
import io.swagger.v3.oas.annotations.tags.Tag;
import java.util.List;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@Tag(name = "Agent Approvals", description = "客服 Agent 人工审批台")
@RestController
@RequestMapping("/api/admin/agent-approvals")
public class AdminAgentApprovalController {

    private final AgentApprovalAdminService approvalService;

    public AdminAgentApprovalController(AgentApprovalAdminService approvalService) {
        this.approvalService = approvalService;
    }

    @GetMapping
    @Operation(summary = "List pending agent approvals")
    public ApiResponse<List<AgentPendingApproval>> list(@AuthenticationPrincipal AccountPrincipal principal) {
        return ApiResponse.success(approvalService.listPending(adminActor(principal)));
    }

    @PostMapping("/{workflowId}")
    @Operation(summary = "Approve or reject an agent workflow")
    public ApiResponse<JsonNode> decide(
        @PathVariable String workflowId,
        @RequestBody AdminAgentApprovalDecisionRequest request,
        @AuthenticationPrincipal AccountPrincipal principal) {
        AgentApprovalDecisionRequest agentRequest = new AgentApprovalDecisionRequest(
            request.getSessionId(),
            request.getDecision(),
            request.getNote()
        );
        return ApiResponse.success(approvalService.decide(adminActor(principal), workflowId, agentRequest));
    }

    private String adminActor(AccountPrincipal principal) {
        if (principal.getUserId() != null && !principal.getUserId().isBlank()) {
            return principal.getUserId();
        }
        return principal.getUsername();
    }
}
