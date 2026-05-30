"""
Claude audit prompt templates.

Production-ready prompt builders for each auditing workflow.
Each function takes an AuditCallRecord (JSONL format) and returns
a tuple of (system_prompt, user_prompt) for the Claude API.

Usage:
    from app.services.audit_prompts import build_full_audit_prompt
    system, user = build_full_audit_prompt(record)

    response = await claude.create_message(
        model="claude-sonnet-4-20250514",
        max_tokens=2000,
        system=system,
        messages=[{"role": "user", "content": user}],
        temperature=0,
    )
"""

from __future__ import annotations

import json
from typing import Any


# ── System prompt (shared) ───────────────────────────────────────────────

SYSTEM_PROMPT = """You are a senior call quality auditor for a Dutch telecommunications company. You audit customer service calls based on transcripts.

RULES:
1. Base ALL analysis on the transcript text provided. Never infer information not present.
2. If the transcript is unclear or incomplete, explicitly say "INSUFFICIENT DATA" for that field.
3. Respond ONLY with the requested JSON structure. No preamble, no commentary.
4. Quote exact phrases from the transcript as evidence for every finding.
5. All scores use a 1-5 scale unless otherwise specified.
6. The transcript language is Dutch. Provide your analysis in English.
7. If confidence_score < 0.80, flag the transcript as LOW_QUALITY and reduce certainty of findings.
8. Never fabricate quotes. If you cannot find evidence, leave the evidence field as null."""


# ── Template builders ────────────────────────────────────────────────────

def build_full_audit_prompt(record: dict) -> tuple[str, str]:
    """
    Build prompt for comprehensive call audit (scorecard).

    Returns: (system_prompt, user_prompt)
    """
    call_context = _build_call_context(record)
    transcript = _get_transcript_text(record)
    confidence = _get_confidence(record)

    user_prompt = f"""{call_context}
Transcript Confidence: {confidence}

TRANSCRIPT:
{transcript}

Audit this call against the following criteria and return a JSON object:

{{
  "call_id": "{record.get('call_id', '')}",
  "agent": "{_get_agent_name(record)}",
  "audit_date": "<ISO timestamp of this audit>",

  "greeting": {{
    "score": "<1-5>",
    "company_identified": "<true|false>",
    "agent_identified": "<true|false>",
    "evidence": "<exact quote>"
  }},

  "problem_identification": {{
    "score": "<1-5>",
    "issue_understood": "<true|false>",
    "clarifying_questions_asked": "<true|false>",
    "customer_acknowledged": "<true|false>",
    "evidence": "<exact quote>"
  }},

  "resolution": {{
    "score": "<1-5>",
    "resolved": "<true|false>",
    "resolution_type": "<resolved|escalated|deferred|unresolved>",
    "next_steps_communicated": "<true|false>",
    "evidence": "<exact quote>"
  }},

  "professionalism": {{
    "score": "<1-5>",
    "polite_language": "<true|false>",
    "empathy_shown": "<true|false>",
    "customer_addressed_by_name": "<true|false>",
    "evidence": "<exact quote>"
  }},

  "closing": {{
    "score": "<1-5>",
    "additional_help_offered": "<true|false>",
    "farewell_appropriate": "<true|false>",
    "evidence": "<exact quote>"
  }},

  "overall_score": "<1-5 weighted average>",
  "flags": ["<list of any concerns>"],
  "summary": "<2-3 sentence summary>",
  "transcript_quality": "<HIGH|MEDIUM|LOW>"
}}"""

    return SYSTEM_PROMPT, user_prompt


def build_escalation_prompt(record: dict) -> tuple[str, str]:
    """Build prompt for escalation detection."""
    call_context = _build_call_context(record)
    transcript = _get_transcript_text(record)

    user_prompt = f"""{call_context}

TRANSCRIPT:
{transcript}

Analyze this call for escalation signals and return:

{{
  "call_id": "{record.get('call_id', '')}",
  "escalation_detected": "<true|false>",
  "severity": "<none|low|medium|high|critical>",

  "signals": [
    {{
      "type": "<complaint|anger|threat|repeat_contact|manager_request|legal_mention|churn_risk|regulatory>",
      "confidence": "<0.0-1.0>",
      "evidence": "<exact quote from transcript>"
    }}
  ],

  "repeat_contact_indicators": {{
    "detected": "<true|false>",
    "evidence": "<exact quote mentioning previous contact>"
  }},

  "manager_request": {{
    "requested": "<true|false>",
    "evidence": "<exact quote>"
  }},

  "compensation_request": {{
    "requested": "<true|false>",
    "type": "<refund|credit|discount|service|other|null>",
    "evidence": "<exact quote>"
  }},

  "recommended_action": "<brief recommendation>",
  "priority": "<P1|P2|P3|P4>"
}}"""

    return SYSTEM_PROMPT, user_prompt


def build_sentiment_prompt(record: dict) -> tuple[str, str]:
    """Build prompt for customer sentiment analysis."""
    call_context = _build_call_context(record)
    transcript = _get_transcript_text(record)
    segments = _get_segments_json(record)

    user_prompt = f"""{call_context}

TRANSCRIPT:
{transcript}

SEGMENTS (for temporal analysis):
{segments}

Analyze customer sentiment throughout this call:

{{
  "call_id": "{record.get('call_id', '')}",

  "overall_sentiment": {{
    "label": "<very_negative|negative|neutral|positive|very_positive>",
    "score": "<-1.0 to 1.0>",
    "confidence": "<0.0-1.0>"
  }},

  "sentiment_trajectory": {{
    "start": "<sentiment label at call opening>",
    "middle": "<sentiment label during main interaction>",
    "end": "<sentiment label at call closing>",
    "trend": "<improving|stable|declining>"
  }},

  "emotional_moments": [
    {{
      "timestamp_seconds": "<approximate>",
      "emotion": "<frustration|anger|relief|gratitude|confusion|satisfaction>",
      "intensity": "<mild|moderate|strong>",
      "evidence": "<exact quote>"
    }}
  ],

  "customer_satisfaction_prediction": {{
    "likely_csat": "<1-5>",
    "reasoning": "<brief explanation based on evidence>"
  }},

  "agent_emotional_impact": {{
    "de_escalation_effective": "<true|false|null>",
    "empathy_moments": ["<exact quotes showing empathy>"],
    "missed_opportunities": ["<moments where empathy could have helped>"]
  }}
}}"""

    return SYSTEM_PROMPT, user_prompt


def build_compliance_prompt(
    record: dict,
    *,
    protocol_rules: list[str] | None = None,
) -> tuple[str, str]:
    """
    Build prompt for compliance & protocol audit.

    Args:
        record: JSONL call record
        protocol_rules: Custom protocol rules (defaults to standard Dutch call center rules)
    """
    if protocol_rules is None:
        protocol_rules = [
            "Agent must identify themselves and the company name",
            "Agent must verify customer identity (name + account number)",
            "Agent must not disclose other customers' information",
            "Agent must not make unauthorized promises (discounts > 15%, contract changes)",
            "Agent must offer further assistance before closing",
            "Agent must document follow-up actions (visible in transcript as verbal confirmation)",
            "Privacy-sensitive data (BSN, creditcard, passwords) must not be read back fully",
        ]

    rules_text = "\n".join(f"  {i+1}. {rule}" for i, rule in enumerate(protocol_rules))

    system = f"""{SYSTEM_PROMPT}

PROTOCOL CHECKLIST:
{rules_text}"""

    call_context = _build_call_context(record)
    transcript = _get_transcript_text(record)

    rule_checks = []
    for rule in protocol_rules:
        rule_id = rule.lower().replace(" ", "_")[:40]
        rule_checks.append(f"""    {{
      "rule": "{rule_id}",
      "status": "<pass|fail|partial|not_applicable>",
      "evidence": "<exact quote or null>"
    }}""")

    checks_json = ",\n".join(rule_checks)

    user_prompt = f"""{call_context}

TRANSCRIPT:
{transcript}

Audit compliance against the protocol checklist:

{{
  "call_id": "{record.get('call_id', '')}",

  "compliance_checks": [
{checks_json}
  ],

  "compliance_score": "<0-100 percentage>",
  "violations": ["<list of rule names that failed>"],
  "risk_level": "<none|low|medium|high|critical>"
}}"""

    return system, user_prompt


def build_classification_prompt(record: dict) -> tuple[str, str]:
    """Build prompt for topic & issue classification."""
    call_context = _build_call_context(record)
    transcript = _get_transcript_text(record)

    user_prompt = f"""{call_context}

TRANSCRIPT:
{transcript}

Classify this call and extract structured data:

{{
  "call_id": "{record.get('call_id', '')}",

  "primary_topic": "<billing|technical_support|account_changes|complaints|information_request|cancellation|new_service|follow_up|other>",
  "secondary_topics": ["<additional topics if applicable>"],

  "issue_details": {{
    "category": "<specific sub-category>",
    "product_mentioned": "<product or service name if mentioned, null otherwise>",
    "account_number_mentioned": "<true|false>",
    "monetary_amount_mentioned": {{
      "detected": "<true|false>",
      "amounts": ["<extracted amounts>"],
      "evidence": "<exact quote>"
    }}
  }},

  "resolution_details": {{
    "resolved_in_call": "<true|false>",
    "actions_taken": ["<list of actions>"],
    "follow_up_required": "<true|false>",
    "follow_up_type": "<callback|technician|email|escalation|null>"
  }},

  "business_signals": {{
    "upsell_opportunity": "<true|false>",
    "churn_risk": "<none|low|medium|high>",
    "process_improvement_signal": "<description if detected, null otherwise>"
  }}
}}"""

    return SYSTEM_PROMPT, user_prompt


# ── Batch audit ──────────────────────────────────────────────────────────

def build_batch_scoring_prompt(
    agent_name: str,
    agent_id: str,
    date_range: str,
    records: list[dict],
) -> tuple[str, str]:
    """
    Build prompt for batch agent scoring (multiple calls at once).

    Optimizes token usage by sending 3-5 calls per request.
    """
    system = f"""{SYSTEM_PROMPT}

You are analyzing a BATCH of calls for a single agent. Provide per-call scores AND an aggregate assessment."""

    calls_text = []
    for i, record in enumerate(records, 1):
        transcript = _get_transcript_text(record)
        calls_text.append(f"""--- CALL {i} ---
Call ID: {record.get('call_id', '')}
Direction: {record.get('direction', 'unknown')}
Duration: {record.get('duration_seconds', 0)}s
Date: {record.get('started_at', 'unknown')}
Transcript:
{transcript}
--- END CALL {i} ---""")

    all_calls = "\n\n".join(calls_text)

    user_prompt = f"""Agent: {agent_name}
Agent ID: {agent_id}
Period: {date_range}

{all_calls}

Return:

{{
  "agent": "{agent_name}",
  "period": "{date_range}",
  "calls_analyzed": {len(records)},

  "per_call_scores": [
    {{
      "call_id": "<id>",
      "overall_score": "<1-5>",
      "resolved": "<true|false>",
      "sentiment_end": "<positive|neutral|negative>",
      "flags": ["<any flags>"]
    }}
  ],

  "aggregate": {{
    "average_score": "<1-5 decimal>",
    "resolution_rate": "<percentage string>",
    "positive_endings": "<percentage string>",
    "top_strengths": ["<max 3>"],
    "areas_for_improvement": ["<max 3>"],
    "coaching_recommendations": ["<specific, actionable items>"]
  }},

  "notable_calls": {{
    "best_call_id": "<id with highest score>",
    "worst_call_id": "<id with lowest score>"
  }}
}}"""

    return system, user_prompt


# ── Token optimization helpers ───────────────────────────────────────────

def trim_transcript(
    record: dict,
    *,
    max_chars: int = 6000,
    remove_low_confidence: bool = True,
) -> str:
    """
    Trim transcript for token-efficient prompting.

    Strategies:
    1. Remove no-speech segments (no_speech_prob > 0.8)
    2. Truncate to max_chars, keeping start + end
    """
    segments = record.get("transcript", {}).get("segments")
    content = _get_transcript_text(record)

    # Strategy 1: Filter by segment quality
    if remove_low_confidence and segments:
        quality_segments = [
            s for s in segments
            if s.get("no_speech_prob", 0) < 0.8
        ]
        if quality_segments:
            content = " ".join(s.get("text", "") for s in quality_segments)

    # Strategy 2: Truncate long transcripts
    if len(content) > max_chars:
        # Keep first 40% and last 40%, add marker
        head_len = int(max_chars * 0.4)
        tail_len = int(max_chars * 0.4)
        content = (
            content[:head_len]
            + "\n\n[... TRANSCRIPT MIDDLE SECTION OMITTED FOR LENGTH ...]\n\n"
            + content[-tail_len:]
        )

    return content


# ── Private helpers ──────────────────────────────────────────────────────

def _build_call_context(record: dict) -> str:
    """Build the call metadata context block."""
    agent_name = _get_agent_name(record)
    return f"""Call ID: {record.get('call_id', 'unknown')}
External ID: {record.get('external_call_id', 'unknown')}
Agent: {agent_name}
Direction: {record.get('direction', 'unknown')}
Duration: {record.get('duration_seconds', 0)} seconds
Date: {record.get('started_at', 'unknown')}"""


def _get_agent_name(record: dict) -> str:
    """Extract agent display name from record."""
    agent = record.get("agent") or {}
    return agent.get("display_name", "Unknown Agent")


def _get_transcript_text(record: dict) -> str:
    """Extract transcript content from record."""
    transcript = record.get("transcript") or {}
    return transcript.get("content", "[NO TRANSCRIPT AVAILABLE]")


def _get_confidence(record: dict) -> str:
    """Extract confidence score as formatted string."""
    transcript = record.get("transcript") or {}
    score = transcript.get("confidence_score")
    if score is not None:
        return f"{score:.2f}"
    return "unknown"


def _get_segments_json(record: dict) -> str:
    """Extract segments as compact JSON string."""
    transcript = record.get("transcript") or {}
    segments = transcript.get("segments") or []
    if not segments:
        return "[no segments available]"
    return json.dumps(segments, ensure_ascii=False, indent=None)
