package com.teachdemo.ecommerce.dto;

import com.fasterxml.jackson.annotation.JsonProperty;

public class AgentResumeRequest {

    @JsonProperty("session_id")
    private final String sessionId;

    @JsonProperty("workflow_id")
    private final String workflowId;

    @JsonProperty("resume_token")
    private final String resumeToken;

    private final String decision;

    @JsonProperty("reviewer_note")
    private final String reviewerNote;

    @JsonProperty("reviewer_id")
    private final String reviewerId;

    @JsonProperty("reviewer_role")
    private final String reviewerRole;

    public AgentResumeRequest(String sessionId, String workflowId, String resumeToken,
                              String decision, String reviewerNote) {
        this(sessionId, workflowId, resumeToken, decision, reviewerNote, "ecommerce-admin", "admin");
    }

    public AgentResumeRequest(String sessionId, String workflowId, String resumeToken,
                              String decision, String reviewerNote,
                              String reviewerId, String reviewerRole) {
        this.sessionId = sessionId;
        this.workflowId = workflowId;
        this.resumeToken = resumeToken;
        this.decision = decision;
        this.reviewerNote = reviewerNote;
        this.reviewerId = reviewerId;
        this.reviewerRole = reviewerRole;
    }

    public String getSessionId() {
        return sessionId;
    }

    public String getWorkflowId() {
        return workflowId;
    }

    public String getResumeToken() {
        return resumeToken;
    }

    public String getDecision() {
        return decision;
    }

    public String getReviewerNote() {
        return reviewerNote;
    }

    public String getReviewerId() {
        return reviewerId;
    }

    public String getReviewerRole() {
        return reviewerRole;
    }
}
