package com.teachdemo.ecommerce.dto;

import com.fasterxml.jackson.annotation.JsonProperty;

public class AgentApprovalDecisionRequest {

    @JsonProperty("session_id")
    private final String sessionId;

    private final String decision;

    private final String note;

    public AgentApprovalDecisionRequest(String sessionId, String decision, String note) {
        this.sessionId = sessionId;
        this.decision = decision;
        this.note = note;
    }

    public String getSessionId() {
        return sessionId;
    }

    public String getDecision() {
        return decision;
    }

    public String getNote() {
        return note;
    }
}
