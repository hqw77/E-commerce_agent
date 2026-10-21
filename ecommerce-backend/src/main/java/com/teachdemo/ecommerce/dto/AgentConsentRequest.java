package com.teachdemo.ecommerce.dto;

import com.fasterxml.jackson.annotation.JsonProperty;

public class AgentConsentRequest {

    @JsonProperty("session_id")
    private final String sessionId;

    @JsonProperty("workflow_id")
    private final String workflowId;

    @JsonProperty("resume_token")
    private final String resumeToken;

    @JsonProperty("user_id")
    private final String userId;

    public AgentConsentRequest(String sessionId, String workflowId, String resumeToken, String userId) {
        this.sessionId = sessionId;
        this.workflowId = workflowId;
        this.resumeToken = resumeToken;
        this.userId = userId;
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

    public String getUserId() {
        return userId;
    }
}
