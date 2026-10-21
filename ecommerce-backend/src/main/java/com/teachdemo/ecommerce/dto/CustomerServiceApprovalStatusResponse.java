package com.teachdemo.ecommerce.dto;

public class CustomerServiceApprovalStatusResponse {

    private final String sessionId;

    private final String workflowId;

    private final String status;

    private final String pendingAction;

    private final String userConsentedAt;

    private final String decidedAt;

    private final String decision;

    private final String reviewerRole;

    private final String message;

    public CustomerServiceApprovalStatusResponse(String sessionId, String workflowId, String status,
                                                 String pendingAction, String userConsentedAt, String decidedAt,
                                                 String decision, String reviewerRole, String message) {
        this.sessionId = sessionId;
        this.workflowId = workflowId;
        this.status = status;
        this.pendingAction = pendingAction;
        this.userConsentedAt = userConsentedAt;
        this.decidedAt = decidedAt;
        this.decision = decision;
        this.reviewerRole = reviewerRole;
        this.message = message;
    }

    public String getSessionId() {
        return sessionId;
    }

    public String getWorkflowId() {
        return workflowId;
    }

    public String getStatus() {
        return status;
    }

    public String getPendingAction() {
        return pendingAction;
    }

    public String getUserConsentedAt() {
        return userConsentedAt;
    }

    public String getDecidedAt() {
        return decidedAt;
    }

    public String getDecision() {
        return decision;
    }

    public String getReviewerRole() {
        return reviewerRole;
    }

    public String getMessage() {
        return message;
    }
}
