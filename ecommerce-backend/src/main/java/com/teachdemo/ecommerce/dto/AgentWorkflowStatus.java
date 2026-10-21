package com.teachdemo.ecommerce.dto;

import com.fasterxml.jackson.annotation.JsonProperty;
import com.fasterxml.jackson.databind.PropertyNamingStrategies;
import com.fasterxml.jackson.databind.annotation.JsonNaming;

@JsonNaming(PropertyNamingStrategies.SnakeCaseStrategy.class)
public class AgentWorkflowStatus {

    @JsonProperty("workflow_id")
    private String workflowId;

    @JsonProperty("session_id")
    private String sessionId;

    private String status;

    @JsonProperty("pending_action")
    private String pendingAction;

    @JsonProperty("order_no")
    private String orderNo;

    @JsonProperty("user_consented_at")
    private String userConsentedAt;

    @JsonProperty("decided_at")
    private String decidedAt;

    private String decision;

    @JsonProperty("reviewer_role")
    private String reviewerRole;

    public String getWorkflowId() {
        return workflowId;
    }

    public void setWorkflowId(String workflowId) {
        this.workflowId = workflowId;
    }

    public String getSessionId() {
        return sessionId;
    }

    public void setSessionId(String sessionId) {
        this.sessionId = sessionId;
    }

    public String getStatus() {
        return status;
    }

    public void setStatus(String status) {
        this.status = status;
    }

    public String getPendingAction() {
        return pendingAction;
    }

    public void setPendingAction(String pendingAction) {
        this.pendingAction = pendingAction;
    }

    public String getOrderNo() {
        return orderNo;
    }

    public void setOrderNo(String orderNo) {
        this.orderNo = orderNo;
    }

    public String getUserConsentedAt() {
        return userConsentedAt;
    }

    public void setUserConsentedAt(String userConsentedAt) {
        this.userConsentedAt = userConsentedAt;
    }

    public String getDecidedAt() {
        return decidedAt;
    }

    public void setDecidedAt(String decidedAt) {
        this.decidedAt = decidedAt;
    }

    public String getDecision() {
        return decision;
    }

    public void setDecision(String decision) {
        this.decision = decision;
    }

    public String getReviewerRole() {
        return reviewerRole;
    }

    public void setReviewerRole(String reviewerRole) {
        this.reviewerRole = reviewerRole;
    }
}
