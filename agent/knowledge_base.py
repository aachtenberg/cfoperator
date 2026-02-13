"""
Knowledge Base for Autonomous SRE Agent - Phase 4 (NO RULES).

PostgreSQL-based knowledge storage using JSONB for flexibility.
Stores system profiles, baselines, drift events, and investigation learnings.

#1 RULE: NO RULES
- No confidence scoring
- No predetermined patterns
- No hardcoded thresholds
- Pure observation and learning
"""

import json
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from contextlib import contextmanager

from sqlalchemy import (
    create_engine, Column, Integer, String, Float, Text, Boolean,
    DateTime, Index, CheckConstraint, text, func, cast
)
from sqlalchemy.types import Text as SQLText
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy import TIMESTAMP
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.pool import QueuePool

Base = declarative_base()


def _log(level: str, msg: str, **fields: Any) -> None:
    """Structured logging."""
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "level": level,
        "msg": msg,
        **fields
    }
    print(json.dumps(payload, ensure_ascii=False))


# ============================= Outcome Normalization ==========================

VALID_OUTCOMES = {"resolved", "escalated", "monitoring", "failed", "in_progress", "retry"}

OUTCOME_ALIASES = {
    "investigating": "in_progress",
    "investigation": "in_progress",
    "ongoing": "in_progress",
    "pending": "in_progress",
    "active": "in_progress",
    "running": "in_progress",
    "fixed": "resolved",
    "complete": "resolved",
    "completed": "resolved",
    "done": "resolved",
    "success": "resolved",
    "successful": "resolved",
    "ok": "resolved",
    "healthy": "resolved",
    "escalate": "escalated",
    "critical": "escalated",
    "urgent": "escalated",
    "alert": "escalated",
    "failure": "failed",
    "error": "failed",
    "broken": "failed",
    "watch": "monitoring",
    "watching": "monitoring",
    "observe": "monitoring",
    "observing": "monitoring",
    "tracked": "monitoring",
    "tracking": "monitoring",
}


def normalize_outcome(outcome: str, default: str = "monitoring") -> str:
    """Normalize LLM-provided outcome to a valid database outcome."""
    if not outcome:
        return default
    outcome_lower = outcome.lower().strip()
    if outcome_lower in VALID_OUTCOMES:
        return outcome_lower
    if outcome_lower in OUTCOME_ALIASES:
        return OUTCOME_ALIASES[outcome_lower]
    _log("warn", "Unknown outcome, using default", raw_outcome=outcome, normalized_to=default)
    return default


# ============================= Schema Models ==================================

class Host(Base):
    """Host registry for multi-host deployments."""
    __tablename__ = 'hosts'

    id = Column(String(64), primary_key=True)  # e.g., 'pi', 'pi2', 'default'
    name = Column(String(255), nullable=False)  # Display name (hostname)
    description = Column(Text)
    first_seen = Column(TIMESTAMP, nullable=False, default=lambda: datetime.now(timezone.utc))
    last_seen = Column(TIMESTAMP, nullable=False, default=lambda: datetime.now(timezone.utc))
    agent_version = Column(String(32))
    host_metadata = Column('metadata', JSONB, default={})  # 'metadata' in DB, 'host_metadata' in Python
    status = Column(String(20), default='active')  # active, inactive, maintenance

    __table_args__ = (
        Index('idx_hosts_last_seen', 'last_seen', postgresql_using='btree', postgresql_ops={'last_seen': 'DESC'}),
        Index('idx_hosts_status', 'status'),
    )


class SystemProfile(Base):
    """System profile from bootstrap discovery."""
    __tablename__ = 'system_profile'

    id = Column(Integer, primary_key=True, autoincrement=True)
    discovered_at = Column(TIMESTAMP, nullable=False, default=lambda: datetime.now(timezone.utc))
    os_type = Column(String(50), nullable=False)  # linux, darwin, windows
    architecture = Column(String(50), nullable=False)  # x86_64, arm64, armv7l
    hostname = Column(String(255))
    profile = Column(JSONB, nullable=False)  # Flexible: services, network, filesystem

    __table_args__ = (
        CheckConstraint("jsonb_typeof(profile) = 'object'", name='valid_profile'),
        Index('idx_system_profile_discovered', 'discovered_at', postgresql_using='btree', postgresql_ops={'discovered_at': 'DESC'}),
        Index('idx_system_profile_gin', 'profile', postgresql_using='gin'),
    )


class SystemPurpose(Base):
    """System purpose learned through investigation - single source of truth."""
    __tablename__ = 'system_purpose'

    id = Column(Integer, primary_key=True, autoincrement=True)
    learned_at = Column(TIMESTAMP, nullable=False, default=lambda: datetime.now(timezone.utc))
    last_updated = Column(TIMESTAMP, nullable=False, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    purpose_description = Column(Text, nullable=False)  # "home automation with ESP sensors"
    purpose_details = Column(JSONB, nullable=False)  # data_flows, critical_services, dependencies

    __table_args__ = (
        CheckConstraint("jsonb_typeof(purpose_details) = 'object'", name='valid_purpose'),
        Index('idx_system_purpose_updated', 'last_updated', postgresql_using='btree', postgresql_ops={'last_updated': 'DESC'}),
        Index('idx_system_purpose_gin', 'purpose_details', postgresql_using='gin'),
    )


class BaselineState(Base):
    """Baseline expectations (what is "normal") per service."""
    __tablename__ = 'baseline_state'

    id = Column(Integer, primary_key=True, autoincrement=True)
    host_id = Column(String(64), nullable=False, default='default')  # Multi-host support
    service_name = Column(String(255), nullable=False)
    established_at = Column(TIMESTAMP, nullable=False, default=lambda: datetime.now(timezone.utc))
    last_updated = Column(TIMESTAMP, nullable=False, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    expected_state = Column(String(50), nullable=False)  # "running", "stopped"
    baseline_metrics = Column(JSONB, nullable=False)  # resource patterns, traffic, error rates

    __table_args__ = (
        CheckConstraint("jsonb_typeof(baseline_metrics) = 'object'", name='valid_metrics'),
        Index('idx_baseline_service', 'service_name'),
        Index('idx_baseline_host', 'host_id'),
        Index('idx_baseline_updated', 'last_updated', postgresql_using='btree', postgresql_ops={'last_updated': 'DESC'}),
        Index('idx_baseline_gin', 'baseline_metrics', postgresql_using='gin'),
        # Unique constraint: service_name unique per host
        {'extend_existing': True}
    )


class DriftEvent(Base):
    """Drift detection (changes from baseline)."""
    __tablename__ = 'drift_events'

    id = Column(Integer, primary_key=True, autoincrement=True)
    host_id = Column(String(64), nullable=False, default='default')  # Multi-host support
    detected_at = Column(TIMESTAMP, nullable=False, default=lambda: datetime.now(timezone.utc))
    drift_type = Column(String(100), nullable=False)  # "new_service", "config_change", "state_change"
    description = Column(Text, nullable=False)
    drift_details = Column(JSONB, nullable=False)  # what changed, why, impact
    investigated = Column(Boolean, default=False)
    purpose_understood = Column(Boolean, default=False)
    baseline_updated = Column(Boolean, default=False)

    __table_args__ = (
        CheckConstraint("jsonb_typeof(drift_details) = 'object'", name='valid_drift'),
        Index('idx_drift_detected', 'detected_at', postgresql_using='btree', postgresql_ops={'detected_at': 'DESC'}),
        Index('idx_drift_type', 'drift_type'),
        Index('idx_drift_host', 'host_id'),
        Index('idx_drift_investigated', 'investigated', postgresql_where=text('investigated = false')),
        Index('idx_drift_gin', 'drift_details', postgresql_using='gin'),
        {'extend_existing': True}
    )


class Investigation(Base):
    """Investigation history (for learning) - complete record of agent reasoning."""
    __tablename__ = 'investigations'

    id = Column(Integer, primary_key=True, autoincrement=True)
    host_id = Column(String(64), nullable=False, default='default')  # Multi-host support
    started_at = Column(TIMESTAMP, nullable=False, default=lambda: datetime.now(timezone.utc))
    completed_at = Column(TIMESTAMP)
    trigger = Column(Text, nullable=False)  # what prompted investigation
    findings = Column(JSONB, nullable=False)  # hypothesis, evidence, actions, learnings
    outcome = Column(String(50), nullable=False)  # "resolved", "escalated", "monitoring", "failed"
    duration_seconds = Column(Float)
    tool_calls_count = Column(Integer, default=0)
    # Triage fields
    parent_investigation_id = Column(Integer, nullable=True)  # For reinvestigations/deep dives
    operator_notes = Column(Text, nullable=True)  # Notes from operator triage
    triage_action = Column(String(50), nullable=True)  # 'retry', 'context', 'resolved', 'suppress', 'ack'

    # Phase 6.2: Root Cause Analysis
    root_cause = Column(JSONB, nullable=True)  # Structured RCA: primary_cause, contributing_factors, etc.
    postmortem_generated = Column(Boolean, default=False)
    postmortem_generated_at = Column(TIMESTAMP, nullable=True)

    __table_args__ = (
        CheckConstraint("jsonb_typeof(findings) = 'object'", name='valid_findings'),
        CheckConstraint("outcome IN ('resolved', 'escalated', 'monitoring', 'failed', 'in_progress', 'retry')", name='valid_outcome'),
        Index('idx_investigations_started', 'started_at', postgresql_using='btree', postgresql_ops={'started_at': 'DESC'}),
        Index('idx_investigations_trigger', 'trigger'),
        Index('idx_investigations_outcome', 'outcome'),
        Index('idx_investigations_host', 'host_id'),
        Index('idx_investigations_gin', 'findings', postgresql_using='gin'),
        {'extend_existing': True}
    )


class InvestigationEvent(Base):
    """Individual events within an investigation - tool calls, reasoning, actions, errors."""
    __tablename__ = 'investigation_events'

    id = Column(Integer, primary_key=True, autoincrement=True)
    investigation_id = Column(Integer, nullable=False)  # References investigations(id)
    event_at = Column(TIMESTAMP, nullable=False, default=lambda: datetime.now(timezone.utc))
    event_type = Column(String(50), nullable=False)  # 'tool_call', 'reasoning', 'action', 'error'

    # For tool calls
    tool_name = Column(String(100))
    tool_input = Column(JSONB)
    tool_output = Column(JSONB)
    duration_ms = Column(Integer)
    success = Column(Boolean, default=True)

    # For reasoning blocks
    reasoning_text = Column(Text)

    # For actions
    action_type = Column(String(100))  # 'restart', 'update_baseline', 'escalate'
    action_target = Column(String(255))

    # For errors
    error_message = Column(Text)

    __table_args__ = (
        CheckConstraint("event_type IN ('tool_call', 'reasoning', 'action', 'error')", name='valid_event_type'),
        Index('idx_inv_events_investigation', 'investigation_id'),
        Index('idx_inv_events_time', 'event_at', postgresql_using='btree', postgresql_ops={'event_at': 'DESC'}),
        Index('idx_inv_events_type', 'event_type'),
    )


class PendingQuestion(Base):
    """Questions from investigations awaiting human response."""
    __tablename__ = 'pending_questions'

    id = Column(Integer, primary_key=True, autoincrement=True)
    investigation_id = Column(Integer, nullable=False)  # References investigations(id)
    host_id = Column(String(64), nullable=False, default='default')
    question = Column(Text, nullable=False)
    context = Column(Text)
    answer = Column(Text)
    asked_at = Column(TIMESTAMP, nullable=False, default=lambda: datetime.now(timezone.utc))
    answered_at = Column(TIMESTAMP)
    status = Column(String(20), nullable=False, default='pending')

    __table_args__ = (
        CheckConstraint("status IN ('pending', 'answered', 'cancelled')", name='valid_question_status'),
        Index('idx_pending_questions_status', 'status'),
        Index('idx_pending_questions_investigation', 'investigation_id'),
        Index('idx_pending_questions_asked', 'asked_at', postgresql_using='btree', postgresql_ops={'asked_at': 'DESC'}),
    )


class AgentSettings(Base):
    """Agent runtime settings - configurable via dashboard."""
    __tablename__ = 'agent_settings'

    key = Column(String(100), primary_key=True)
    value = Column(Text, nullable=False)
    updated_at = Column(TIMESTAMP, nullable=False, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index('idx_agent_settings_updated', 'updated_at'),
    )


class InvestigationQueue(Base):
    """Queue for investigations requested via dashboard triage actions."""
    __tablename__ = 'investigation_queue'

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(TIMESTAMP, nullable=False, default=lambda: datetime.now(timezone.utc))
    investigation_id = Column(Integer, nullable=True)  # Parent investigation being triaged
    queue_type = Column(String(50), nullable=False)  # 'retry', 'context', 'service_check'
    target_service = Column(String(255), nullable=True)  # For service-specific queues
    operator_context = Column(Text, nullable=True)  # Context/notes from operator (the KEY value!)
    priority = Column(Integer, default=5)  # 1=highest, 10=lowest
    status = Column(String(50), default='pending')  # 'pending', 'processing', 'completed', 'cancelled'
    processed_at = Column(TIMESTAMP, nullable=True)
    result_investigation_id = Column(Integer, nullable=True)  # Investigation created from this queue item

    __table_args__ = (
        CheckConstraint("queue_type IN ('retry', 'context', 'service_check')", name='valid_queue_type'),
        CheckConstraint("status IN ('pending', 'processing', 'completed', 'cancelled')", name='valid_queue_status'),
        Index('idx_queue_status', 'status'),
        Index('idx_queue_created', 'created_at', postgresql_using='btree', postgresql_ops={'created_at': 'DESC'}),
        Index('idx_queue_priority', 'priority'),
    )


class SuppressionPattern(Base):
    """Patterns to suppress alerts - learned from false positive triages."""
    __tablename__ = 'suppression_patterns'

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(TIMESTAMP, nullable=False, default=lambda: datetime.now(timezone.utc))
    service_name = Column(String(255), nullable=False)  # e.g., 'influxdb3-core'
    trigger_pattern = Column(String(500), nullable=False)  # substring match, e.g., 'health check failing'
    reason = Column(Text, nullable=True)  # Why this is being suppressed
    created_from_investigation_id = Column(Integer, nullable=True)  # Which investigation led to this
    active = Column(Boolean, default=True)
    expires_at = Column(TIMESTAMP, nullable=True)  # Optional auto-expiry

    __table_args__ = (
        Index('idx_suppress_service', 'service_name'),
        Index('idx_suppress_active', 'active', postgresql_where=text('active = true')),
    )


class DataFreshnessCheck(Base):
    """Configurable data freshness checks - observe pipeline health without auto-acting.

    Supports multiple check types:
    - influxdb_query: Query InfluxDB for latest timestamp per measurement
    - prometheus_metric: Check metric staleness via Prometheus
    - redis_key: Check Redis key TTL/age
    - http_endpoint: Check if endpoint returns recent data
    - mqtt_topic: Check last message timestamp on MQTT topic
    """
    __tablename__ = 'data_freshness_checks'

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), nullable=False)  # Human-readable name, e.g., "ESP Temperature Sensors"
    check_type = Column(String(50), nullable=False)  # influxdb_query, prometheus_metric, redis_key, http_endpoint, mqtt_topic
    description = Column(Text, nullable=True)  # What this check monitors

    # Check configuration - varies by type
    # influxdb_query: {db: "temperature_data", measurement: "esp_temperature", time_field: "time"}
    # prometheus_metric: {metric_name: "up", labels: {job: "mqtt"}, staleness_query: "..."}
    # redis_key: {key_pattern: "sensor:*:last_reading", host: "redis:6379"}
    # http_endpoint: {url: "http://service/health", json_path: "$.last_updated"}
    # mqtt_topic: {topic: "esp-sensor-hub/+/temperature", broker: "mosquitto"}
    check_config = Column(JSONB, nullable=False)

    # Threshold configuration
    stale_threshold_minutes = Column(Integer, nullable=False, default=10)  # Minutes before considered stale
    critical_threshold_minutes = Column(Integer, nullable=True)  # Optional: escalate if stale beyond this

    # Status tracking
    enabled = Column(Boolean, nullable=False, default=True)
    last_check_at = Column(TIMESTAMP, nullable=True)
    last_check_result = Column(JSONB, nullable=True)  # {success: bool, data_age_minutes: float, error: str}

    # Metadata
    created_at = Column(TIMESTAMP, nullable=False, default=lambda: datetime.now(timezone.utc), server_default=text('NOW()'))
    created_by = Column(String(100), nullable=True)  # user or 'auto-discovered'
    tags = Column(JSONB, nullable=True)  # For grouping, e.g., {"pipeline": "esp-sensors", "tier": "critical"}

    __table_args__ = (
        CheckConstraint("check_type IN ('influxdb_query', 'prometheus_metric', 'redis_key', 'http_endpoint', 'mqtt_topic')", name='valid_check_type'),
        CheckConstraint("jsonb_typeof(check_config) = 'object'", name='valid_check_config'),
        CheckConstraint("stale_threshold_minutes > 0", name='positive_threshold'),
        Index('idx_freshness_enabled', 'enabled', postgresql_where=text('enabled = true')),
        Index('idx_freshness_type', 'check_type'),
        Index('idx_freshness_last_check', 'last_check_at', postgresql_using='btree', postgresql_ops={'last_check_at': 'DESC'}),
    )


class Postmortem(Base):
    """Auto-generated postmortem reports - Phase 6.2 Root Cause Analysis."""
    __tablename__ = 'postmortems'

    id = Column(Integer, primary_key=True, autoincrement=True)
    investigation_id = Column(Integer, nullable=False)  # References investigations(id)
    host_id = Column(String(64), nullable=False, default='default')
    generated_at = Column(TIMESTAMP, nullable=False, default=lambda: datetime.now(timezone.utc))

    # Timeline
    incident_start = Column(TIMESTAMP, nullable=False)
    incident_end = Column(TIMESTAMP, nullable=True)
    detection_time_seconds = Column(Float, nullable=True)  # Time from start to detection
    resolution_time_seconds = Column(Float, nullable=True)  # Time from detection to resolution

    # Impact
    services_affected = Column(JSONB, nullable=True)  # List of service names
    impact_summary = Column(Text, nullable=True)
    severity = Column(String(20), nullable=True)  # critical, high, medium, low

    # Root cause analysis
    root_cause = Column(JSONB, nullable=True)  # Structured RCA from investigation

    # Timeline of events
    timeline = Column(JSONB, nullable=True)  # Ordered list of events with timestamps

    # Actions taken
    actions_taken = Column(JSONB, nullable=True)  # What was done
    action_effectiveness = Column(JSONB, nullable=True)  # Did each action help?

    # Prevention
    prevention_recommendations = Column(JSONB, nullable=True)
    follow_up_tasks = Column(JSONB, nullable=True)

    # Status
    status = Column(String(20), nullable=False, default='draft')  # draft, reviewed, published
    reviewed_by = Column(String(100), nullable=True)
    reviewed_at = Column(TIMESTAMP, nullable=True)

    __table_args__ = (
        CheckConstraint("severity IN ('critical', 'high', 'medium', 'low')", name='valid_pm_severity'),
        CheckConstraint("status IN ('draft', 'reviewed', 'published')", name='valid_pm_status'),
        Index('idx_postmortem_investigation', 'investigation_id'),
        Index('idx_postmortem_generated', 'generated_at', postgresql_using='btree', postgresql_ops={'generated_at': 'DESC'}),
        Index('idx_postmortem_severity', 'severity'),
        Index('idx_postmortem_status', 'status'),
    )


class ConfigChange(Base):
    """Configuration change proposals and history - Phase 5 Self-Healing."""
    __tablename__ = 'config_changes'

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(TIMESTAMP, nullable=False, default=lambda: datetime.now(timezone.utc))
    config_type = Column(String(50), nullable=False)  # 'docker-compose', 'prometheus-rules', 'telegraf'
    target_file = Column(String(500), nullable=False)  # Full path to config file
    change_type = Column(String(50), nullable=False)  # 'resource_limit', 'alert_rule', 'health_check'
    service = Column(String(255), nullable=False)  # Service affected
    description = Column(Text, nullable=False)  # Human-readable description
    change_spec = Column(JSONB, nullable=False)  # Specification of the change
    reason = Column(Text, nullable=False)  # Why this change is needed
    proposed_diff = Column(Text, nullable=True)  # Unified diff preview
    original_content = Column(Text, nullable=True)  # Backup of original content
    status = Column(String(20), nullable=False, default='proposed')  # 'proposed', 'approved', 'applied', 'rejected', 'rolled_back', 'failed'
    applied_at = Column(TIMESTAMP, nullable=True)
    applied_by = Column(String(100), nullable=True)  # 'auto' or 'user:<name>'
    verification_result = Column(JSONB, nullable=True)  # Result of post-apply verification
    backup_path = Column(String(500), nullable=True)  # Path to backup file
    investigation_id = Column(Integer, nullable=True)  # Investigation that triggered this
    dedup_key = Column(String(255), nullable=True)  # MD5 hash for deduplication

    __table_args__ = (
        CheckConstraint("jsonb_typeof(change_spec) = 'object'", name='valid_change_spec'),
        CheckConstraint("status IN ('proposed', 'approved', 'applied', 'rejected', 'rolled_back', 'failed')", name='valid_config_status'),
        Index('idx_config_changes_status', 'status'),
        Index('idx_config_changes_created', 'created_at', postgresql_using='btree', postgresql_ops={'created_at': 'DESC'}),
        Index('idx_config_changes_type', 'config_type'),
        Index('idx_config_changes_service', 'service'),
        Index('idx_config_changes_dedup', 'dedup_key', 'status'),
    )


class NotificationChannel(Base):
    """Notification channel configuration - Phase 5.5 Alerting."""
    __tablename__ = 'notification_channels'

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(TIMESTAMP, nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(TIMESTAMP, nullable=False, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    channel_type = Column(String(50), nullable=False)  # 'slack', 'pagerduty', 'email', 'ntfy'
    name = Column(String(255), nullable=False)  # User-friendly name
    enabled = Column(Boolean, nullable=False, default=True)
    config = Column(JSONB, nullable=False)  # Channel-specific config (webhook_url, api_key, etc.)
    severity_filter = Column(String(20), nullable=False, default='warning')  # Minimum severity: 'info', 'warning', 'critical'
    last_success_at = Column(TIMESTAMP, nullable=True)
    last_failure_at = Column(TIMESTAMP, nullable=True)
    failure_count = Column(Integer, nullable=False, default=0)

    __table_args__ = (
        CheckConstraint("jsonb_typeof(config) = 'object'", name='valid_channel_config'),
        CheckConstraint("channel_type IN ('slack', 'pagerduty', 'email', 'ntfy')", name='valid_channel_type'),
        CheckConstraint("severity_filter IN ('info', 'warning', 'critical')", name='valid_severity_filter'),
        Index('idx_notification_channels_type', 'channel_type'),
        Index('idx_notification_channels_enabled', 'enabled', postgresql_where=text('enabled = true')),
    )


class NotificationHistory(Base):
    """History of sent notifications for audit trail."""
    __tablename__ = 'notification_history'

    id = Column(Integer, primary_key=True, autoincrement=True)
    sent_at = Column(TIMESTAMP, nullable=False, default=lambda: datetime.now(timezone.utc))
    channel_id = Column(Integer, nullable=False)  # FK to notification_channels
    channel_type = Column(String(50), nullable=False)  # Denormalized for queries
    severity = Column(String(20), nullable=False)  # 'info', 'warning', 'critical'
    title = Column(String(500), nullable=False)
    message = Column(Text, nullable=False)
    context = Column(JSONB, nullable=True)  # Additional context (investigation_id, etc.)
    success = Column(Boolean, nullable=False)
    error_message = Column(Text, nullable=True)
    read_at = Column(TIMESTAMP, nullable=True)  # When notification was marked as read

    __table_args__ = (
        CheckConstraint("severity IN ('info', 'warning', 'critical')", name='valid_notification_severity'),
        Index('idx_notification_history_sent', 'sent_at', postgresql_using='btree', postgresql_ops={'sent_at': 'DESC'}),
        Index('idx_notification_history_channel', 'channel_id'),
        Index('idx_notification_history_severity', 'severity'),
    )


class MetricSnapshot(Base):
    """Metric snapshots captured during investigations - Phase 6 Correlation Analysis."""
    __tablename__ = 'metric_snapshots'

    id = Column(Integer, primary_key=True, autoincrement=True)
    captured_at = Column(TIMESTAMP, nullable=False, default=lambda: datetime.now(timezone.utc))
    host_id = Column(String(64), nullable=False, default='default')
    investigation_id = Column(Integer, nullable=True)  # FK to investigations (optional)
    snapshot_type = Column(String(50), nullable=False)  # 'health_check', 'investigation_start', 'investigation_end', 'anomaly'
    metrics = Column(JSONB, nullable=False)  # cpu, memory, disk, network, mqtt, service_latencies

    __table_args__ = (
        CheckConstraint("jsonb_typeof(metrics) = 'object'", name='valid_metrics'),
        CheckConstraint("snapshot_type IN ('health_check', 'investigation_start', 'investigation_end', 'anomaly', 'periodic')", name='valid_snapshot_type'),
        Index('idx_metric_snapshots_captured', 'captured_at', postgresql_using='btree', postgresql_ops={'captured_at': 'DESC'}),
        Index('idx_metric_snapshots_investigation', 'investigation_id'),
        Index('idx_metric_snapshots_type', 'snapshot_type'),
        Index('idx_metric_snapshots_host', 'host_id'),
        Index('idx_metric_snapshots_gin', 'metrics', postgresql_using='gin'),
    )


class ServiceCorrelation(Base):
    """Learned service correlations - which services fail together - Phase 6."""
    __tablename__ = 'service_correlations'

    id = Column(Integer, primary_key=True, autoincrement=True)
    first_seen = Column(TIMESTAMP, nullable=False, default=lambda: datetime.now(timezone.utc))
    last_seen = Column(TIMESTAMP, nullable=False, default=lambda: datetime.now(timezone.utc))
    service_a = Column(String(255), nullable=False)  # Primary service
    service_b = Column(String(255), nullable=False)  # Correlated service
    correlation_type = Column(String(50), nullable=False)  # 'cascade_failure', 'co_failure', 'dependency', 'resource_contention'
    occurrence_count = Column(Integer, nullable=False, default=1)
    avg_time_delta_seconds = Column(Float)  # Average time between service_a and service_b events
    correlation_details = Column(JSONB, nullable=False, default={})  # Additional context

    __table_args__ = (
        CheckConstraint("jsonb_typeof(correlation_details) = 'object'", name='valid_correlation_details'),
        CheckConstraint("correlation_type IN ('cascade_failure', 'co_failure', 'dependency', 'resource_contention')", name='valid_correlation_type'),
        Index('idx_service_correlations_services', 'service_a', 'service_b'),
        Index('idx_service_correlations_type', 'correlation_type'),
        Index('idx_service_correlations_count', 'occurrence_count', postgresql_using='btree', postgresql_ops={'occurrence_count': 'DESC'}),
        Index('idx_service_correlations_last', 'last_seen', postgresql_using='btree', postgresql_ops={'last_seen': 'DESC'}),
    )


class EventCorrelation(Base):
    """Correlated events found during analysis - Phase 6."""
    __tablename__ = 'event_correlations'

    id = Column(Integer, primary_key=True, autoincrement=True)
    detected_at = Column(TIMESTAMP, nullable=False, default=lambda: datetime.now(timezone.utc))
    correlation_window_seconds = Column(Integer, nullable=False)  # Time window used for correlation
    event_a_type = Column(String(50), nullable=False)  # 'investigation', 'drift', 'metric_anomaly'
    event_a_id = Column(Integer, nullable=False)
    event_b_type = Column(String(50), nullable=False)
    event_b_id = Column(Integer, nullable=False)
    time_delta_seconds = Column(Float)  # Time between events (negative = B before A)
    correlation_strength = Column(Float)  # 0.0 to 1.0 based on frequency
    root_cause_candidate = Column(String(50))  # Which event is likely root cause: 'event_a', 'event_b', 'unknown'
    analysis_notes = Column(Text)

    __table_args__ = (
        CheckConstraint("event_a_type IN ('investigation', 'drift', 'metric_anomaly')", name='valid_event_a_type'),
        CheckConstraint("event_b_type IN ('investigation', 'drift', 'metric_anomaly')", name='valid_event_b_type'),
        Index('idx_event_correlations_detected', 'detected_at', postgresql_using='btree', postgresql_ops={'detected_at': 'DESC'}),
        Index('idx_event_correlations_events', 'event_a_type', 'event_a_id', 'event_b_type', 'event_b_id'),
        Index('idx_event_correlations_strength', 'correlation_strength', postgresql_using='btree', postgresql_ops={'correlation_strength': 'DESC'}),
    )


class ScheduledJob(Base):
    """Scheduled jobs for the cron scheduler."""
    __tablename__ = 'scheduled_jobs'

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False)
    description = Column(Text)
    enabled = Column(Boolean, nullable=False, default=True)
    created_at = Column(TIMESTAMP, nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(TIMESTAMP, nullable=False, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    # Schedule type: 'interval', 'cron', 'once'
    schedule_type = Column(String(20), nullable=False)
    # For interval: seconds between runs
    interval_seconds = Column(Integer)
    # For cron: cron expression (e.g., "0 3 * * *" for 3am daily)
    cron_expr = Column(String(100))
    # For cron: timezone (e.g., "America/New_York")
    cron_tz = Column(String(50))
    # For once: timestamp to run at
    run_at = Column(TIMESTAMP)

    # Job type: 'monitoring_cycle', 'deep_analysis', 'custom_prompt'
    job_type = Column(String(50), nullable=False)
    # For custom_prompt: the prompt to send to the agent
    custom_prompt = Column(Text)
    # Optional host_id filter (null = all hosts)
    host_id = Column(String(64))

    # Execution state
    next_run_at = Column(TIMESTAMP)
    last_run_at = Column(TIMESTAMP)
    last_status = Column(String(20))  # 'ok', 'error', 'skipped', 'running'
    last_error = Column(Text)
    last_duration_seconds = Column(Float)
    run_count = Column(Integer, default=0)

    __table_args__ = (
        CheckConstraint("schedule_type IN ('interval', 'cron', 'once')", name='valid_schedule_type'),
        CheckConstraint("job_type IN ('monitoring_cycle', 'deep_analysis', 'custom_prompt')", name='valid_job_type'),
        Index('idx_scheduled_jobs_next_run', 'next_run_at', postgresql_using='btree'),
        Index('idx_scheduled_jobs_enabled', 'enabled'),
        Index('idx_scheduled_jobs_host', 'host_id'),
    )


class InvestigationLearning(Base):
    """
    Structured learnings extracted from investigations.

    Stores reusable patterns, insights, and solutions discovered during
    investigations. Unlike raw investigation data, these are distilled
    knowledge that can be directly applied to future issues.

    Inspired by OpenClaw's structured memory system.
    """
    __tablename__ = 'investigation_learnings'

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(TIMESTAMP, nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(TIMESTAMP, nullable=False, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    # Source tracking
    investigation_id = Column(Integer, nullable=True)  # Which investigation produced this
    host_id = Column(String(64), nullable=False, default='default')

    # Learning classification
    learning_type = Column(String(50), nullable=False)  # 'pattern', 'solution', 'root_cause', 'antipattern', 'insight'

    # Core content
    title = Column(String(500), nullable=False)  # Brief description
    description = Column(Text, nullable=False)  # Detailed explanation
    applies_when = Column(Text)  # Conditions when this learning applies
    solution_steps = Column(JSONB)  # For solution type: ordered steps

    # Categorization
    services = Column(JSONB)  # List of services this applies to
    tags = Column(JSONB)  # Flexible tags: ["memory", "restart", "docker"]
    category = Column(String(100))  # High-level category: "resource", "network", "config", "dependency"

    # Effectiveness tracking
    times_applied = Column(Integer, default=0)  # How many times this was used
    times_successful = Column(Integer, default=0)  # How many times it helped
    success_rate = Column(Float)  # Computed: times_successful / times_applied

    # Search vectors (for hybrid search)
    search_text = Column(Text)  # Combined text for FTS
    embedding_hash = Column(String(64))  # MD5 of search_text for cache dedup

    # Status
    verified = Column(Boolean, default=False)  # Manually verified as useful
    deprecated = Column(Boolean, default=False)  # No longer applicable

    __table_args__ = (
        CheckConstraint(
            "learning_type IN ('pattern', 'solution', 'root_cause', 'antipattern', 'insight')",
            name='valid_learning_type'
        ),
        Index('idx_learnings_type', 'learning_type'),
        Index('idx_learnings_created', 'created_at', postgresql_using='btree', postgresql_ops={'created_at': 'DESC'}),
        Index('idx_learnings_host', 'host_id'),
        Index('idx_learnings_investigation', 'investigation_id'),
        Index('idx_learnings_category', 'category'),
        Index('idx_learnings_success', 'success_rate', postgresql_using='btree', postgresql_ops={'success_rate': 'DESC'}),
        Index('idx_learnings_services_gin', 'services', postgresql_using='gin'),
        Index('idx_learnings_tags_gin', 'tags', postgresql_using='gin'),
    )


class LLMProviderState(Base):
    """
    Tracks LLM provider health and cooldown state.

    Used by LLMFallbackManager to persist cooldown state across agent restarts.
    Enables exponential backoff when providers fail (timeout, rate limit, etc).
    """
    __tablename__ = 'llm_provider_state'

    provider_key = Column(String(255), primary_key=True)  # e.g., "ollama/localhost/qwen3:14b"
    cooldown_until = Column(TIMESTAMP, nullable=True)  # NULL if not in cooldown
    error_count = Column(Integer, default=0)
    last_error_at = Column(TIMESTAMP, nullable=True)
    last_error_reason = Column(String(50), nullable=True)  # 'timeout', 'rate_limit', 'auth', 'connection'
    last_success_at = Column(TIMESTAMP, nullable=True)
    updated_at = Column(TIMESTAMP, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class SweepReport(Base):
    """Proactive sweep results — stored per sweep cycle."""
    __tablename__ = 'sweep_reports'

    id = Column(Integer, primary_key=True, autoincrement=True)
    host_id = Column(String(64), nullable=False, default='default')
    swept_at = Column(TIMESTAMP, nullable=False, default=lambda: datetime.now(timezone.utc))
    severity = Column(String(20), nullable=False)  # 'info', 'warning', 'critical'
    finding_count = Column(Integer, nullable=False, default=0)
    findings = Column(JSONB, nullable=False)  # list of {severity, finding, sweep_phase}
    summary = Column(Text, nullable=False)
    sweep_meta = Column(JSONB, nullable=True)  # provider, model, token usage, durations

    __table_args__ = (
        Index('idx_sweep_swept_at', 'swept_at', postgresql_using='btree', postgresql_ops={'swept_at': 'DESC'}),
        Index('idx_sweep_severity', 'severity'),
        Index('idx_sweep_host', 'host_id'),
        Index('idx_sweep_findings_gin', 'findings', postgresql_using='gin'),
        {'extend_existing': True}
    )


# ============================= Connection Health Monitor ======================

class ConnectionHealthMonitor:
    """
    Fast PostgreSQL connection health monitoring.

    Used by ResilientKnowledgeBase to detect connection issues quickly
    and trigger failover to local buffering.
    """

    def __init__(self, engine, check_interval: int = 10, failure_threshold: int = 2):
        """
        Initialize health monitor.

        Args:
            engine: SQLAlchemy engine to monitor
            check_interval: Seconds between health checks (cached)
            failure_threshold: Consecutive failures before marking unhealthy
        """
        self.engine = engine
        self.check_interval = check_interval
        self.failure_threshold = failure_threshold
        self._last_check = 0.0
        self._is_healthy = True
        self._consecutive_failures = 0
        self._lock = threading.Lock()

    def is_healthy(self) -> bool:
        """
        Check if connection is healthy (cached with TTL).

        Returns:
            True if database is reachable, False otherwise
        """
        now = time.time()
        if now - self._last_check < self.check_interval:
            return self._is_healthy

        with self._lock:
            # Double-check after acquiring lock
            if now - self._last_check < self.check_interval:
                return self._is_healthy

            self._last_check = now
            try:
                # Fast check - just test connection
                with self.engine.connect() as conn:
                    conn.execute(text("SELECT 1"))
                    conn.commit()
                if not self._is_healthy:
                    _log("info", "PostgreSQL connection restored",
                         previous_failures=self._consecutive_failures)
                self._is_healthy = True
                self._consecutive_failures = 0
            except Exception as e:
                self._consecutive_failures += 1
                # Only mark unhealthy after threshold consecutive failures
                if self._consecutive_failures >= self.failure_threshold:
                    if self._is_healthy:
                        _log("warn", "PostgreSQL marked unhealthy",
                             error=str(e),
                             consecutive_failures=self._consecutive_failures)
                    self._is_healthy = False

        return self._is_healthy

    def mark_unhealthy(self):
        """Immediately mark connection as unhealthy (called on operation failure)."""
        with self._lock:
            self._is_healthy = False
            self._consecutive_failures += 1
            _log("warn", "PostgreSQL marked unhealthy (operation failure)",
                 consecutive_failures=self._consecutive_failures)

    def get_status(self) -> Dict[str, Any]:
        """Get current health status."""
        return {
            "healthy": self._is_healthy,
            "consecutive_failures": self._consecutive_failures,
            "last_check": self._last_check
        }


# ============================= Knowledge Base =================================

class KnowledgeBase:
    """
    Autonomous SRE Knowledge Base - Phase 4.

    Stores system profiles, baselines, drift events, and investigation learnings.
    NO RULES. Pure observation and learning.
    """

    def __init__(self, db_url: Optional[str] = None, host_id: Optional[str] = None):
        """Initialize knowledge base with PostgreSQL connection."""
        if db_url is None:
            # Read from environment
            db_type = os.getenv("KNOWLEDGE_BASE_DB_TYPE", "postgresql")
            if db_type != "postgresql":
                raise ValueError(f"Phase 4 requires PostgreSQL, got: {db_type}")

            host = os.getenv("KNOWLEDGE_BASE_PG_HOST", "localhost")
            port = os.getenv("KNOWLEDGE_BASE_PG_PORT", "5432")
            database = os.getenv("KNOWLEDGE_BASE_PG_DATABASE", "sre_knowledge")
            user = os.getenv("KNOWLEDGE_BASE_PG_USER", "sre_agent")
            password = os.getenv("KNOWLEDGE_BASE_PG_PASSWORD", "")

            db_url = f"postgresql://{user}:{password}@{host}:{port}/{database}"

        # Host ID for multi-host support (defaults to 'default' for backwards compatibility)
        if host_id is None:
            host_id = os.getenv("SENTINEL_HOST_ID", "default")
        self.host_id = host_id

        self.db_url = db_url
        self.engine = create_engine(
            db_url,
            poolclass=QueuePool,
            pool_size=5,
            max_overflow=10,
            pool_pre_ping=True,
        )
        self.Session = sessionmaker(bind=self.engine)

        _log("info", "Knowledge base initialized", db_type="postgresql", host_id=self.host_id)

    def initialize_schema(self):
        """Create all tables if they don't exist."""
        Base.metadata.create_all(self.engine)
        # Ensure FTS columns exist (added after initial schema)
        self.ensure_fts_schema()
        # Ensure learning_embeddings table exists for semantic search on learnings
        self._ensure_learning_embeddings_table()
        _log("info", "Knowledge base schema initialized")

    def _ensure_learning_embeddings_table(self):
        """Create learning_embeddings table if it doesn't exist."""
        try:
            from sqlalchemy import text
            with self.session_scope() as session:
                session.execute(text("""
                    CREATE TABLE IF NOT EXISTS learning_embeddings (
                        id SERIAL PRIMARY KEY,
                        learning_id INTEGER NOT NULL UNIQUE REFERENCES investigation_learnings(id) ON DELETE CASCADE,
                        embedding vector(768),
                        embedding_model VARCHAR(100) NOT NULL,
                        embedding_text TEXT NOT NULL,
                        created_at TIMESTAMPTZ DEFAULT NOW()
                    )
                """))
                session.execute(text("""
                    CREATE INDEX IF NOT EXISTS idx_learning_embeddings_lid
                    ON learning_embeddings (learning_id)
                """))
                session.execute(text("""
                    CREATE INDEX IF NOT EXISTS idx_learning_embeddings_vector
                    ON learning_embeddings USING hnsw (embedding vector_cosine_ops)
                    WITH (m = 16, ef_construction = 64)
                """))
                session.commit()
        except Exception as e:
            _log("debug", "learning_embeddings table setup", note=str(e))

    def register_host(self, name: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None):
        """Register or update this host in the hosts table."""
        import socket
        if name is None:
            # Try HOST_HOSTNAME env var first (set by docker-compose), then socket.gethostname()
            name = os.getenv("HOST_HOSTNAME") or socket.gethostname()
        if metadata is None:
            metadata = {}

        with self.session_scope() as session:
            # Upsert host record
            result = session.execute(text("""
                INSERT INTO hosts (id, name, metadata, first_seen, last_seen)
                VALUES (:id, :name, :metadata, NOW(), NOW())
                ON CONFLICT (id) DO UPDATE SET
                    name = EXCLUDED.name,
                    last_seen = NOW(),
                    metadata = hosts.metadata || EXCLUDED.metadata
                RETURNING id
            """), {
                'id': self.host_id,
                'name': name,
                'metadata': json.dumps(metadata)
            })
            _log("info", "Host registered", host_id=self.host_id, name=name)

    def heartbeat(self):
        """Update last_seen timestamp for this host. Also re-registers if host was deleted."""
        # Just call register_host - it uses upsert so it will create if missing or update if exists
        self.register_host()

    def get_host_config(self) -> Dict[str, Any]:
        """Get configuration for this host from the metadata.config field.

        Returns configuration dict with defaults:
        - display_name: Friendly display name
        - excluded_services: List of services to exclude from health checks
        - health_check_urls: Dict of service -> URL overrides
        - monitoring_enabled: Whether monitoring is enabled for this host
        - notes: Optional notes about this host
        """
        with self.session_scope() as session:
            result = session.execute(text("""
                SELECT metadata FROM hosts WHERE id = :host_id
            """), {'host_id': self.host_id}).fetchone()

            if not result or not result[0]:
                return {
                    'display_name': '',
                    'excluded_services': [],
                    'health_check_urls': {},
                    'monitoring_enabled': True,
                    'notes': ''
                }

            metadata = result[0]
            if isinstance(metadata, str):
                try:
                    metadata = json.loads(metadata)
                except json.JSONDecodeError:
                    metadata = {}

            config = metadata.get('config', {})
            return {
                'display_name': config.get('display_name', ''),
                'excluded_services': config.get('excluded_services', []),
                'health_check_urls': config.get('health_check_urls', {}),
                'monitoring_enabled': config.get('monitoring_enabled', True),
                'notes': config.get('notes', '')
            }

    @contextmanager
    def session_scope(self):
        """Provide a transactional scope for database operations."""
        session = self.Session()
        try:
            yield session
            session.commit()
        except Exception as e:
            session.rollback()
            _log("error", "Database transaction failed", error=str(e))
            raise
        finally:
            session.close()

    # ========================= Bootstrap & Discovery ==========================

    def record_system_profile(self, os_type: str, architecture: str, hostname: str, profile: Dict[str, Any]) -> int:
        """Record system profile from bootstrap discovery."""
        with self.session_scope() as session:
            system_profile = SystemProfile(
                os_type=os_type,
                architecture=architecture,
                hostname=hostname,
                profile=profile
            )
            session.add(system_profile)
            session.flush()
            profile_id = system_profile.id
            _log("info", "System profile recorded", profile_id=profile_id, os=os_type, arch=architecture)
            return profile_id

    def get_latest_system_profile(self) -> Optional[Dict[str, Any]]:
        """Get the most recent system profile."""
        with self.session_scope() as session:
            profile = session.query(SystemProfile).order_by(SystemProfile.discovered_at.desc()).first()
            if profile:
                return {
                    "id": profile.id,
                    "discovered_at": profile.discovered_at.isoformat(),
                    "os_type": profile.os_type,
                    "architecture": profile.architecture,
                    "hostname": profile.hostname,
                    "profile": profile.profile
                }
            return None

    def update_system_purpose(self, purpose_description: str, purpose_details: Dict[str, Any]) -> int:
        """Update or create system purpose."""
        with self.session_scope() as session:
            # Get existing purpose (should be only one)
            purpose = session.query(SystemPurpose).first()

            if purpose:
                purpose.purpose_description = purpose_description
                purpose.purpose_details = purpose_details
                purpose.last_updated = datetime.now(timezone.utc)
                purpose_id = purpose.id
                _log("info", "System purpose updated", purpose_id=purpose_id)
            else:
                purpose = SystemPurpose(
                    purpose_description=purpose_description,
                    purpose_details=purpose_details
                )
                session.add(purpose)
                session.flush()
                purpose_id = purpose.id
                _log("info", "System purpose created", purpose_id=purpose_id)

            return purpose_id

    def get_system_purpose(self) -> Optional[Dict[str, Any]]:
        """Get current system purpose."""
        with self.session_scope() as session:
            purpose = session.query(SystemPurpose).first()
            if purpose:
                return {
                    "id": purpose.id,
                    "learned_at": purpose.learned_at.isoformat(),
                    "last_updated": purpose.last_updated.isoformat(),
                    "purpose_description": purpose.purpose_description,
                    "purpose_details": purpose.purpose_details
                }
            return None

    # ============================= Baseline Management =========================

    def update_baseline(self, service_name: str, expected_state: str, baseline_metrics: Dict[str, Any]) -> int:
        """Update or create baseline for a service (scoped to this host)."""
        with self.session_scope() as session:
            # Query scoped to this host
            baseline = session.query(BaselineState).filter_by(
                host_id=self.host_id,
                service_name=service_name
            ).first()

            if baseline:
                baseline.expected_state = expected_state
                baseline.baseline_metrics = baseline_metrics
                baseline.last_updated = datetime.now(timezone.utc)
                baseline_id = baseline.id
                _log("info", "Baseline updated", host_id=self.host_id, service=service_name, state=expected_state)
            else:
                baseline = BaselineState(
                    host_id=self.host_id,
                    service_name=service_name,
                    expected_state=expected_state,
                    baseline_metrics=baseline_metrics
                )
                session.add(baseline)
                session.flush()
                baseline_id = baseline.id
                _log("info", "Baseline created", host_id=self.host_id, service=service_name, state=expected_state)

            return baseline_id

    def get_baseline(self, service_name: Optional[str] = None) -> Dict[str, Any]:
        """Get baseline for a service or all services (scoped to this host)."""
        with self.session_scope() as session:
            if service_name:
                baseline = session.query(BaselineState).filter_by(
                    host_id=self.host_id,
                    service_name=service_name
                ).first()
                if baseline:
                    return {
                        "service_name": baseline.service_name,
                        "established_at": baseline.established_at.isoformat(),
                        "last_updated": baseline.last_updated.isoformat(),
                        "expected_state": baseline.expected_state,
                        "baseline_metrics": baseline.baseline_metrics
                    }
                return {}
            else:
                # Get all baselines for this host
                baselines = session.query(BaselineState).filter_by(host_id=self.host_id).all()
                return {
                    b.service_name: {
                        "established_at": b.established_at.isoformat(),
                        "last_updated": b.last_updated.isoformat(),
                        "expected_state": b.expected_state,
                        "baseline_metrics": b.baseline_metrics
                    }
                    for b in baselines
                }

    # ============================= Drift Detection ============================

    def record_drift_event(self, drift_type: str, description: str, drift_details: Dict[str, Any]) -> int:
        """Record a drift event (scoped to this host)."""
        with self.session_scope() as session:
            drift = DriftEvent(
                host_id=self.host_id,
                drift_type=drift_type,
                description=description,
                drift_details=drift_details
            )
            session.add(drift)
            session.flush()
            drift_id = drift.id
            _log("info", "Drift event recorded", host_id=self.host_id, drift_id=drift_id, type=drift_type)
            return drift_id

    def get_uninvestigated_drift_events(self) -> List[Dict[str, Any]]:
        """Get drift events that haven't been investigated yet."""
        with self.session_scope() as session:
            drifts = session.query(DriftEvent).filter_by(investigated=False).order_by(DriftEvent.detected_at.desc()).all()
            return [
                {
                    "id": d.id,
                    "detected_at": d.detected_at.isoformat(),
                    "drift_type": d.drift_type,
                    "description": d.description,
                    "drift_details": d.drift_details
                }
                for d in drifts
            ]

    def mark_drift_investigated(self, drift_id: int, purpose_understood: bool, baseline_updated: bool):
        """Mark a drift event as investigated."""
        with self.session_scope() as session:
            drift = session.query(DriftEvent).filter_by(id=drift_id).first()
            if drift:
                drift.investigated = True
                drift.purpose_understood = purpose_understood
                drift.baseline_updated = baseline_updated
                _log("info", "Drift marked investigated", drift_id=drift_id)

    def bulk_acknowledge_drift(self, drift_type: Optional[str] = None, before_id: Optional[int] = None) -> int:
        """Bulk acknowledge drift events. Returns count of acknowledged events."""
        with self.session_scope() as session:
            query = session.query(DriftEvent).filter_by(investigated=False)
            if drift_type:
                query = query.filter_by(drift_type=drift_type)
            if before_id:
                query = query.filter(DriftEvent.id <= before_id)
            count = query.update({
                DriftEvent.investigated: True,
                DriftEvent.purpose_understood: True
            }, synchronize_session=False)
            _log("info", "Bulk acknowledged drift", count=count, type=drift_type)
            return count

    # ============================= Sweep Reports =============================

    def store_sweep_report(self, severity: str, findings: List[Dict[str, Any]],
                           summary: str, sweep_meta: Optional[Dict[str, Any]] = None) -> int:
        """Store a sweep report."""
        with self.session_scope() as session:
            report = SweepReport(
                host_id=self.host_id,
                severity=severity,
                finding_count=len(findings),
                findings=findings,
                summary=summary,
                sweep_meta=sweep_meta or {}
            )
            session.add(report)
            session.flush()
            report_id = report.id
            _log("info", "Sweep report stored", report_id=report_id, severity=severity, findings=len(findings))
            return report_id

    def get_recent_sweep_reports(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Get recent sweep reports, newest first."""
        with self.session_scope() as session:
            reports = session.query(SweepReport).order_by(
                SweepReport.swept_at.desc()
            ).limit(limit).all()
            return [
                {
                    'id': r.id,
                    'swept_at': r.swept_at.isoformat(),
                    'severity': r.severity,
                    'finding_count': r.finding_count,
                    'findings': r.findings,
                    'summary': r.summary,
                    'sweep_meta': r.sweep_meta
                }
                for r in reports
            ]

    def get_sweep_report(self, report_id: int) -> Optional[Dict[str, Any]]:
        """Get a single sweep report by ID."""
        with self.session_scope() as session:
            r = session.query(SweepReport).filter(SweepReport.id == report_id).first()
            if not r:
                return None
            return {
                'id': r.id,
                'swept_at': r.swept_at.isoformat(),
                'severity': r.severity,
                'finding_count': r.finding_count,
                'findings': r.findings,
                'summary': r.summary,
                'sweep_meta': r.sweep_meta
            }

    def update_sweep_finding(self, report_id: int, finding_index: int,
                             status: str, resolution: str = '') -> bool:
        """Update a specific finding within a sweep report.

        Args:
            report_id: Sweep report ID
            finding_index: Index of the finding in the findings array (0-based)
            status: New status (e.g., 'resolved', 'acknowledged', 'investigating', 'false_positive')
            resolution: Optional resolution note

        Returns:
            True if updated successfully
        """
        with self.session_scope() as session:
            report = session.query(SweepReport).filter(SweepReport.id == report_id).first()
            if not report:
                _log("warning", "Sweep report not found", report_id=report_id)
                return False

            findings = list(report.findings)  # copy JSONB
            if finding_index < 0 or finding_index >= len(findings):
                _log("warning", "Finding index out of range", report_id=report_id, index=finding_index)
                return False

            findings[finding_index]['status'] = status
            if resolution:
                findings[finding_index]['resolution'] = resolution
            report.findings = findings
            # Force SQLAlchemy to detect JSONB change
            from sqlalchemy.orm.attributes import flag_modified
            flag_modified(report, 'findings')

            _log("info", "Sweep finding updated", report_id=report_id,
                 index=finding_index, status=status)
            return True

    def get_operational_summary(self, hours: int = 24) -> Dict[str, Any]:
        """Get aggregate stats across sweeps, investigations, and learnings for a time window."""
        from datetime import timedelta
        from sqlalchemy import func

        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=hours)

        with self.session_scope() as session:
            # --- Sweeps ---
            sweep_rows = session.query(SweepReport).filter(SweepReport.swept_at >= cutoff).all()
            sweep_total = len(sweep_rows)
            sweep_severity = {}
            total_findings = 0
            for r in sweep_rows:
                total_findings += r.finding_count
                sweep_severity[r.severity] = sweep_severity.get(r.severity, 0) + 1
            recent_sweeps = sorted(sweep_rows, key=lambda r: r.swept_at, reverse=True)[:5]

            # --- Investigations ---
            inv_rows = session.query(Investigation).filter(Investigation.started_at >= cutoff).all()
            inv_total = len(inv_rows)
            inv_outcomes = {}
            durations = []
            tool_calls_total = 0
            for inv in inv_rows:
                inv_outcomes[inv.outcome] = inv_outcomes.get(inv.outcome, 0) + 1
                if inv.duration_seconds:
                    durations.append(inv.duration_seconds)
                tool_calls_total += inv.tool_calls_count or 0
            recent_invs = sorted(inv_rows, key=lambda i: i.started_at, reverse=True)[:5]

            # --- Learnings ---
            learn_rows = session.query(InvestigationLearning).filter(
                InvestigationLearning.created_at >= cutoff
            ).all()
            learn_total = len(learn_rows)
            learn_types = {}
            for l in learn_rows:
                learn_types[l.learning_type] = learn_types.get(l.learning_type, 0) + 1

        return {
            "time_window": {
                "hours": hours,
                "from_utc": cutoff.isoformat(),
                "to_utc": now.isoformat()
            },
            "sweeps": {
                "total": sweep_total,
                "total_findings": total_findings,
                "avg_findings": round(total_findings / sweep_total, 1) if sweep_total else 0,
                "by_severity": sweep_severity,
                "recent": [
                    {
                        "id": r.id,
                        "swept_at": r.swept_at.isoformat(),
                        "severity": r.severity,
                        "finding_count": r.finding_count,
                        "summary": (r.summary or "")[:150]
                    }
                    for r in recent_sweeps
                ]
            },
            "investigations": {
                "total": inv_total,
                "by_outcome": inv_outcomes,
                "avg_duration_seconds": round(sum(durations) / len(durations), 1) if durations else None,
                "total_tool_calls": tool_calls_total,
                "recent": [
                    {
                        "id": inv.id,
                        "started_at": inv.started_at.isoformat(),
                        "trigger": (inv.trigger or "")[:150],
                        "outcome": inv.outcome,
                        "duration_seconds": inv.duration_seconds
                    }
                    for inv in recent_invs
                ]
            },
            "learnings": {
                "total": learn_total,
                "by_type": learn_types
            }
        }

    def get_human_responses(self, unprocessed_only: bool = True) -> List[Dict[str, Any]]:
        """Get human responses to input requests.

        Returns drift events of type 'human_input_requested' that have
        a human_response in their drift_details.

        Args:
            unprocessed_only: If True, only return responses not yet marked as investigated

        Returns:
            List of dicts with id, question, context, human_response, response_at
        """
        with self.session_scope() as session:
            query = session.query(DriftEvent).filter_by(drift_type='human_input_requested')
            if unprocessed_only:
                query = query.filter_by(investigated=False)
            query = query.order_by(DriftEvent.detected_at.desc())

            responses = []
            for d in query.all():
                details = d.drift_details or {}
                if details.get('human_response'):
                    responses.append({
                        'id': d.id,
                        'detected_at': d.detected_at.isoformat() if d.detected_at else None,
                        'question': details.get('question', d.description),
                        'context': details.get('context', ''),
                        'human_response': details.get('human_response', ''),
                        'response_at': details.get('response_at', '')
                    })

            return responses

    # ============================= Investigations =============================

    def start_investigation(self, trigger: str) -> int:
        """Start an investigation and return its ID for event tracking (scoped to this host)."""
        with self.session_scope() as session:
            investigation = Investigation(
                host_id=self.host_id,
                trigger=trigger,
                findings={},  # Empty until completion
                outcome='in_progress',  # Will be updated when complete
            )
            session.add(investigation)
            session.flush()
            inv_id = investigation.id
            _log("info", "Investigation started", host_id=self.host_id, investigation_id=inv_id, trigger=trigger[:100])
            return inv_id

    def update_investigation(
        self,
        investigation_id: int,
        findings: Dict[str, Any],
        outcome: str,
        duration_seconds: Optional[float] = None,
        tool_calls_count: int = 0
    ) -> None:
        """Update an investigation with final findings and outcome."""
        normalized_outcome = normalize_outcome(outcome)
        with self.session_scope() as session:
            investigation = session.query(Investigation).filter(
                Investigation.id == investigation_id
            ).first()
            if investigation:
                investigation.findings = findings
                investigation.outcome = normalized_outcome
                investigation.duration_seconds = duration_seconds
                investigation.tool_calls_count = tool_calls_count
                investigation.completed_at = datetime.now(timezone.utc)
                _log("info", "Investigation updated", investigation_id=investigation_id, outcome=normalized_outcome)

    def record_investigation(
        self,
        trigger: str,
        findings: Dict[str, Any],
        outcome: str,
        duration_seconds: Optional[float] = None,
        tool_calls_count: int = 0,
        parent_investigation_id: Optional[int] = None,
        host_id: Optional[str] = None
    ) -> int:
        """Record an investigation (creates record, optionally linked to parent).

        Args:
            host_id: If not provided, uses self.host_id. For context/retry investigations,
                     pass the parent's host_id to maintain proper AOR tracking.
        """
        normalized_outcome = normalize_outcome(outcome)
        effective_host_id = host_id or self.host_id
        with self.session_scope() as session:
            investigation = Investigation(
                trigger=trigger,
                findings=findings,
                outcome=normalized_outcome,
                duration_seconds=duration_seconds,
                tool_calls_count=tool_calls_count,
                parent_investigation_id=parent_investigation_id,
                completed_at=datetime.now(timezone.utc) if normalized_outcome != 'in_progress' else None,
                host_id=effective_host_id
            )
            session.add(investigation)
            session.flush()
            inv_id = investigation.id
            _log("info", "Investigation recorded", investigation_id=inv_id, outcome=normalized_outcome,
                 parent_id=parent_investigation_id, host_id=effective_host_id)
            return inv_id

    def update_investigation(
        self,
        investigation_id: int,
        completed_at: Optional[datetime] = None,
        findings: Optional[Dict[str, Any]] = None,
        outcome: Optional[str] = None,
        duration_seconds: Optional[float] = None,
        tool_calls_count: Optional[int] = None
    ) -> bool:
        """Update an existing investigation record."""
        with self.session_scope() as session:
            inv = session.query(Investigation).filter_by(id=investigation_id).first()
            if inv:
                if completed_at is not None:
                    inv.completed_at = completed_at
                if findings is not None:
                    inv.findings = findings
                if outcome is not None:
                    inv.outcome = normalize_outcome(outcome)
                if duration_seconds is not None:
                    inv.duration_seconds = duration_seconds
                if tool_calls_count is not None:
                    inv.tool_calls_count = tool_calls_count
                _log("info", "Investigation updated", investigation_id=investigation_id)
                return True
            return False

    def record_investigation_event(
        self,
        investigation_id: int,
        event_type: str,
        tool_name: Optional[str] = None,
        tool_input: Optional[Dict[str, Any]] = None,
        tool_output: Optional[Any] = None,
        duration_ms: Optional[int] = None,
        success: bool = True,
        reasoning_text: Optional[str] = None,
        action_type: Optional[str] = None,
        action_target: Optional[str] = None,
        error_message: Optional[str] = None
    ) -> int:
        """Record an event within an investigation (tool call, reasoning, action, error)."""
        with self.session_scope() as session:
            # Truncate large outputs to avoid DB bloat
            if tool_output is not None:
                output_str = str(tool_output) if not isinstance(tool_output, (dict, list)) else json.dumps(tool_output)
                if len(output_str) > 10000:
                    tool_output = {"truncated": True, "preview": output_str[:2000], "full_length": len(output_str)}

            event = InvestigationEvent(
                investigation_id=investigation_id,
                event_type=event_type,
                tool_name=tool_name,
                tool_input=tool_input,
                tool_output=tool_output if isinstance(tool_output, (dict, list, type(None))) else {"result": str(tool_output)},
                duration_ms=duration_ms,
                success=success,
                reasoning_text=reasoning_text,
                action_type=action_type,
                action_target=action_target,
                error_message=error_message
            )
            session.add(event)
            session.flush()
            return event.id

    def get_investigation_events(self, investigation_id: int) -> List[Dict[str, Any]]:
        """Get all events for an investigation in chronological order."""
        with self.session_scope() as session:
            events = session.query(InvestigationEvent).filter(
                InvestigationEvent.investigation_id == investigation_id
            ).order_by(InvestigationEvent.event_at.asc()).all()

            return [
                {
                    "id": e.id,
                    "event_at": e.event_at.isoformat() if e.event_at else None,
                    "event_type": e.event_type,
                    "tool_name": e.tool_name,
                    "tool_input": e.tool_input,
                    "tool_output": e.tool_output,
                    "duration_ms": e.duration_ms,
                    "success": e.success,
                    "reasoning_text": e.reasoning_text,
                    "action_type": e.action_type,
                    "action_target": e.action_target,
                    "error_message": e.error_message
                }
                for e in events
            ]

    # ========================= Pending Questions (Q&A) =========================

    def store_question(self, investigation_id: int, question: str, context: str = "") -> int:
        """Store a question from an investigation awaiting human response."""
        with self.session_scope() as session:
            pending_q = PendingQuestion(
                investigation_id=investigation_id,
                host_id=self.host_id,
                question=question,
                context=context,
                status='pending'
            )
            session.add(pending_q)
            session.flush()
            q_id = pending_q.id
            _log("info", "Question stored", question_id=q_id, investigation_id=investigation_id)
            return q_id

    def get_pending_questions(self, host_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get all pending questions, optionally filtered by host."""
        with self.session_scope() as session:
            query = session.query(PendingQuestion).filter(
                PendingQuestion.status == 'pending'
            )

            if host_id:
                query = query.filter(PendingQuestion.host_id == host_id)

            questions = query.order_by(PendingQuestion.asked_at.desc()).all()

            return [
                {
                    'id': q.id,
                    'investigation_id': q.investigation_id,
                    'host_id': q.host_id,
                    'question': q.question,
                    'context': q.context,
                    'asked_at': q.asked_at.isoformat() if q.asked_at else None
                }
                for q in questions
            ]

    def answer_question(self, question_id: int, answer: str) -> Optional[int]:
        """Mark a question as answered and return the investigation_id."""
        with self.session_scope() as session:
            question = session.query(PendingQuestion).filter(
                PendingQuestion.id == question_id
            ).first()

            if question:
                question.answer = answer
                question.answered_at = datetime.now(timezone.utc)
                question.status = 'answered'
                inv_id = question.investigation_id
                _log("info", "Question answered", question_id=question_id, investigation_id=inv_id)
                return inv_id
            else:
                _log("warn", "Question not found", question_id=question_id)
                return None

    # ========================= Investigation Search & Similarity =========================

    def query_similar_investigations(self, search_text: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Full-text search for similar investigations."""
        with self.session_scope() as session:
            # PostgreSQL full-text search
            results = session.query(Investigation).filter(
                func.to_tsvector('english', Investigation.findings).op('@@')(func.to_tsquery('english', search_text))
            ).order_by(Investigation.started_at.desc()).limit(limit).all()

            return [
                {
                    "id": inv.id,
                    "started_at": inv.started_at.isoformat(),
                    "trigger": inv.trigger,
                    "findings": inv.findings,
                    "outcome": inv.outcome,
                    "duration_seconds": inv.duration_seconds
                }
                for inv in results
            ]

    # ============================= Semantic Search (pgvector) ===================

    def store_investigation_embedding(
        self,
        investigation_id: int,
        embedding: List[float],
        embedding_model: str,
        embedding_text: str
    ) -> bool:
        """
        Store embedding for an investigation.

        Args:
            investigation_id: ID of the investigation
            embedding: Vector embedding as list of floats
            embedding_model: Name of the model used to generate embedding
            embedding_text: The text that was embedded

        Returns:
            True if stored successfully, False otherwise
        """
        with self.session_scope() as session:
            try:
                # Use raw SQL for pgvector insertion
                # pgvector accepts array format: '[0.1, 0.2, ...]'
                embedding_str = "[" + ",".join(str(v) for v in embedding) + "]"
                # Insert/update embedding and FTS vector together
                session.execute(text("""
                    INSERT INTO investigation_embeddings
                        (investigation_id, embedding, embedding_model, embedding_text, search_tsv)
                    VALUES
                        (:inv_id, :embedding, :model, :text, to_tsvector('english', :text))
                    ON CONFLICT (investigation_id)
                    DO UPDATE SET
                        embedding = EXCLUDED.embedding,
                        embedding_model = EXCLUDED.embedding_model,
                        embedding_text = EXCLUDED.embedding_text,
                        search_tsv = to_tsvector('english', EXCLUDED.embedding_text),
                        created_at = NOW()
                """), {
                    'inv_id': investigation_id,
                    'embedding': embedding_str,
                    'model': embedding_model,
                    'text': embedding_text
                })
                _log("info", "Investigation embedding stored",
                     investigation_id=investigation_id,
                     model=embedding_model)
                return True
            except Exception as e:
                _log("error", "Failed to store embedding",
                     investigation_id=investigation_id,
                     error=str(e))
                return False

    def find_similar_investigations_by_vector(
        self,
        query_embedding: List[float],
        limit: int = 5,
        min_similarity: float = 0.5,
        exclude_investigation_id: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        Find similar investigations using vector similarity search.

        Args:
            query_embedding: Query vector to find similar investigations
            limit: Maximum number of results
            min_similarity: Minimum cosine similarity (0-1)
            exclude_investigation_id: Exclude this investigation from results

        Returns:
            List of similar investigations with similarity scores
        """
        with self.session_scope() as session:
            try:
                # Build query with optional exclusion
                exclude_clause = ""
                # Format embedding as pgvector string literal
                embedding_str = "[" + ",".join(str(v) for v in query_embedding) + "]"
                params = {
                    'limit': limit,
                    'min_sim': min_similarity
                }

                if exclude_investigation_id:
                    exclude_clause = "AND ie.investigation_id != :exclude_id"
                    params['exclude_id'] = exclude_investigation_id

                # pgvector cosine distance: <=> returns distance (0=identical)
                # similarity = 1 - distance
                # Note: Use string interpolation for embedding since ::vector cast conflicts with SQLAlchemy params
                results = session.execute(text(f"""
                    SELECT
                        i.id,
                        i.trigger,
                        i.findings,
                        i.outcome,
                        i.started_at,
                        i.duration_seconds,
                        ie.embedding_text,
                        1 - (ie.embedding <=> '{embedding_str}'::vector) as similarity
                    FROM investigation_embeddings ie
                    JOIN investigations i ON ie.investigation_id = i.id
                    WHERE 1 - (ie.embedding <=> '{embedding_str}'::vector) >= :min_sim
                    {exclude_clause}
                    ORDER BY ie.embedding <=> '{embedding_str}'::vector
                    LIMIT :limit
                """), params).fetchall()

                return [
                    {
                        "id": row[0],
                        "trigger": row[1],
                        "findings": row[2],
                        "outcome": row[3],
                        "started_at": row[4].isoformat() if row[4] else None,
                        "duration_seconds": row[5],
                        "embedding_text": row[6],
                        "similarity": round(float(row[7]), 3)
                    }
                    for row in results
                ]
            except Exception as e:
                _log("error", "Similar investigation search failed", error=str(e))
                return []

    def has_investigation_embedding(self, investigation_id: int) -> bool:
        """Check if an investigation already has an embedding."""
        with self.session_scope() as session:
            try:
                result = session.execute(text("""
                    SELECT 1 FROM investigation_embeddings
                    WHERE investigation_id = :inv_id
                """), {'inv_id': investigation_id}).fetchone()
                return result is not None
            except Exception:
                # Table might not exist yet
                return False

    def get_embedding_stats(self) -> Dict[str, Any]:
        """Get statistics about stored embeddings."""
        with self.session_scope() as session:
            try:
                total = session.execute(text(
                    "SELECT COUNT(*) FROM investigation_embeddings"
                )).scalar() or 0

                inv_count = session.execute(text(
                    "SELECT COUNT(*) FROM investigations"
                )).scalar() or 0

                models = session.execute(text("""
                    SELECT embedding_model, COUNT(*)
                    FROM investigation_embeddings
                    GROUP BY embedding_model
                """)).fetchall()

                return {
                    "total_embeddings": total,
                    "total_investigations": inv_count,
                    "coverage_pct": round(total / inv_count * 100, 1) if inv_count > 0 else 0,
                    "models_used": {row[0]: row[1] for row in models}
                }
            except Exception as e:
                # Table might not exist yet
                return {
                    "total_embeddings": 0,
                    "total_investigations": 0,
                    "coverage_pct": 0,
                    "models_used": {},
                    "error": str(e)
                }

    def get_recent_investigations(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Get recent investigations."""
        with self.session_scope() as session:
            investigations = session.query(Investigation).order_by(Investigation.started_at.desc()).limit(limit).all()
            return [
                {
                    "id": inv.id,
                    "started_at": inv.started_at.isoformat(),
                    "completed_at": inv.completed_at.isoformat() if inv.completed_at else None,
                    "trigger": inv.trigger,
                    "outcome": inv.outcome,
                    "duration_seconds": inv.duration_seconds,
                    "tool_calls_count": inv.tool_calls_count
                }
                for inv in investigations
            ]

    # ============================= Hybrid Search (Vector + FTS) =============

    def ensure_fts_schema(self) -> bool:
        """
        Ensure full-text search column and index exist on investigation_embeddings.

        Adds search_tsv column (tsvector) for PostgreSQL full-text search.
        This enables hybrid search combining vector similarity with keyword matching.
        """
        with self.session_scope() as session:
            try:
                # Check if column exists
                result = session.execute(text("""
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_name = 'investigation_embeddings'
                    AND column_name = 'search_tsv'
                """)).fetchone()

                if not result:
                    _log("info", "Adding FTS column to investigation_embeddings")
                    # Add tsvector column
                    session.execute(text("""
                        ALTER TABLE investigation_embeddings
                        ADD COLUMN search_tsv tsvector
                    """))

                    # Create GIN index for fast FTS queries
                    session.execute(text("""
                        CREATE INDEX IF NOT EXISTS idx_inv_embeddings_fts
                        ON investigation_embeddings USING GIN(search_tsv)
                    """))

                    # Populate from existing embedding_text
                    session.execute(text("""
                        UPDATE investigation_embeddings
                        SET search_tsv = to_tsvector('english', COALESCE(embedding_text, ''))
                        WHERE search_tsv IS NULL
                    """))

                    _log("info", "FTS schema added and populated")
                return True
            except Exception as e:
                _log("error", "Failed to ensure FTS schema", error=str(e))
                return False

    def update_fts_vector(self, investigation_id: int, search_text: str) -> bool:
        """Update FTS vector for an investigation embedding."""
        with self.session_scope() as session:
            try:
                session.execute(text("""
                    UPDATE investigation_embeddings
                    SET search_tsv = to_tsvector('english', :text)
                    WHERE investigation_id = :inv_id
                """), {'inv_id': investigation_id, 'text': search_text})
                return True
            except Exception as e:
                _log("error", "Failed to update FTS vector", error=str(e), investigation_id=investigation_id)
                return False

    def find_similar_investigations_hybrid(
        self,
        query_text: str,
        query_embedding: Optional[List[float]] = None,
        limit: int = 5,
        vector_weight: float = 0.7,
        fts_weight: float = 0.3,
        min_score: float = 0.1,
        exclude_investigation_id: Optional[int] = None,
        host_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Find similar investigations using hybrid search (vector + FTS).

        Combines semantic similarity from embeddings with keyword matching from
        PostgreSQL full-text search. Results are ranked by weighted combination.

        Args:
            query_text: Query text for FTS (and embedding if not provided)
            query_embedding: Pre-computed query embedding (optional)
            limit: Maximum results to return
            vector_weight: Weight for vector similarity (0-1)
            fts_weight: Weight for FTS score (0-1)
            min_score: Minimum combined score threshold
            exclude_investigation_id: Exclude this investigation from results
            host_id: Filter to specific host (optional)

        Returns:
            List of investigations with combined scores
        """
        with self.session_scope() as session:
            try:
                params = {
                    'limit': limit * 3,  # Fetch more to re-rank
                    'query_text': query_text,
                    'vector_weight': vector_weight,
                    'fts_weight': fts_weight,
                    'min_score': min_score
                }

                # Build WHERE clauses
                where_clauses = []
                if exclude_investigation_id:
                    where_clauses.append("ie.investigation_id != :exclude_id")
                    params['exclude_id'] = exclude_investigation_id
                if host_id:
                    where_clauses.append("i.host_id = :host_id")
                    params['host_id'] = host_id

                where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"

                # If we have an embedding, do hybrid search
                if query_embedding and len(query_embedding) > 0:
                    embedding_str = "[" + ",".join(str(v) for v in query_embedding) + "]"

                    results = session.execute(text(f"""
                        WITH vector_scores AS (
                            SELECT
                                ie.investigation_id,
                                1 - (ie.embedding <=> '{embedding_str}'::vector) as vector_sim
                            FROM investigation_embeddings ie
                            JOIN investigations i ON ie.investigation_id = i.id
                            WHERE {where_sql}
                        ),
                        fts_scores AS (
                            SELECT
                                ie.investigation_id,
                                ts_rank_cd(ie.search_tsv, plainto_tsquery('english', :query_text)) as fts_rank
                            FROM investigation_embeddings ie
                            JOIN investigations i ON ie.investigation_id = i.id
                            WHERE ie.search_tsv @@ plainto_tsquery('english', :query_text)
                            AND {where_sql}
                        )
                        SELECT
                            i.id,
                            i.trigger,
                            i.findings,
                            i.outcome,
                            i.started_at,
                            i.duration_seconds,
                            i.host_id,
                            ie.embedding_text,
                            COALESCE(v.vector_sim, 0) as vector_sim,
                            COALESCE(f.fts_rank, 0) as fts_rank,
                            (COALESCE(v.vector_sim, 0) * :vector_weight +
                             COALESCE(f.fts_rank, 0) * :fts_weight) as combined_score
                        FROM investigation_embeddings ie
                        JOIN investigations i ON ie.investigation_id = i.id
                        LEFT JOIN vector_scores v ON ie.investigation_id = v.investigation_id
                        LEFT JOIN fts_scores f ON ie.investigation_id = f.investigation_id
                        WHERE (v.vector_sim IS NOT NULL OR f.fts_rank IS NOT NULL)
                        AND {where_sql}
                        ORDER BY combined_score DESC
                        LIMIT :limit
                    """), params).fetchall()
                else:
                    # FTS-only search (no embedding available)
                    results = session.execute(text(f"""
                        SELECT
                            i.id,
                            i.trigger,
                            i.findings,
                            i.outcome,
                            i.started_at,
                            i.duration_seconds,
                            i.host_id,
                            ie.embedding_text,
                            0 as vector_sim,
                            ts_rank_cd(ie.search_tsv, plainto_tsquery('english', :query_text)) as fts_rank,
                            ts_rank_cd(ie.search_tsv, plainto_tsquery('english', :query_text)) as combined_score
                        FROM investigation_embeddings ie
                        JOIN investigations i ON ie.investigation_id = i.id
                        WHERE ie.search_tsv @@ plainto_tsquery('english', :query_text)
                        AND {where_sql}
                        ORDER BY fts_rank DESC
                        LIMIT :limit
                    """), params).fetchall()

                # Filter by minimum score and limit
                output = []
                for row in results:
                    combined_score = float(row[10]) if row[10] else 0
                    if combined_score >= min_score:
                        output.append({
                            "id": row[0],
                            "trigger": row[1],
                            "findings": row[2],
                            "outcome": row[3],
                            "started_at": row[4].isoformat() if row[4] else None,
                            "duration_seconds": row[5],
                            "host_id": row[6],
                            "embedding_text": row[7],
                            "vector_similarity": round(float(row[8]), 3) if row[8] else 0,
                            "fts_rank": round(float(row[9]), 3) if row[9] else 0,
                            "combined_score": round(combined_score, 3)
                        })
                        if len(output) >= limit:
                            break

                return output
            except Exception as e:
                _log("error", "Hybrid search failed", error=str(e))
                # Fallback to vector-only if hybrid fails
                if query_embedding:
                    return self.find_similar_investigations_by_vector(
                        query_embedding, limit=limit, exclude_investigation_id=exclude_investigation_id
                    )
                return []

    def get_unindexed_investigations(self, limit: int = 100) -> List[Dict[str, Any]]:
        """
        Get investigations that don't have embeddings yet.

        Returns:
            List of investigation dicts needing embedding
        """
        with self.session_scope() as session:
            try:
                results = session.execute(text("""
                    SELECT i.id, i.trigger, i.findings, i.outcome, i.started_at
                    FROM investigations i
                    LEFT JOIN investigation_embeddings ie ON i.id = ie.investigation_id
                    WHERE ie.investigation_id IS NULL
                    AND i.outcome IN ('resolved', 'escalated', 'failed')
                    ORDER BY i.started_at DESC
                    LIMIT :limit
                """), {'limit': limit}).fetchall()

                return [
                    {
                        "id": row[0],
                        "trigger": row[1],
                        "findings": row[2],
                        "outcome": row[3],
                        "started_at": row[4].isoformat() if row[4] else None
                    }
                    for row in results
                ]
            except Exception as e:
                _log("error", "Failed to get unindexed investigations", error=str(e))
                return []

    def get_investigations_missing_fts(self, limit: int = 100) -> List[int]:
        """
        Get investigation IDs that have embeddings but missing FTS vectors.

        Returns:
            List of investigation IDs needing FTS update
        """
        with self.session_scope() as session:
            try:
                results = session.execute(text("""
                    SELECT investigation_id
                    FROM investigation_embeddings
                    WHERE search_tsv IS NULL
                    LIMIT :limit
                """), {'limit': limit}).fetchall()
                return [row[0] for row in results]
            except Exception as e:
                _log("error", "Failed to get investigations missing FTS", error=str(e))
                return []

    def get_unindexed_learnings(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Get learnings that don't have embeddings yet."""
        with self.session_scope() as session:
            try:
                results = session.execute(text("""
                    SELECT il.id, il.title, il.description, il.applies_when,
                           il.category, il.services, il.tags
                    FROM investigation_learnings il
                    LEFT JOIN learning_embeddings le ON il.id = le.learning_id
                    WHERE le.learning_id IS NULL
                    AND il.deprecated = false
                    ORDER BY il.created_at DESC
                    LIMIT :limit
                """), {'limit': limit}).fetchall()

                return [
                    {
                        "id": row[0],
                        "title": row[1],
                        "description": row[2],
                        "applies_when": row[3],
                        "category": row[4],
                        "services": row[5],
                        "tags": row[6],
                    }
                    for row in results
                ]
            except Exception as e:
                _log("error", "Failed to get unindexed learnings", error=str(e))
                return []

    # ============================= Settings =================================

    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Get a setting value by key."""
        with self.session_scope() as session:
            setting = session.query(AgentSettings).filter_by(key=key).first()
            return setting.value if setting else default

    def set_setting(self, key: str, value: str) -> None:
        """Set a setting value (upsert)."""
        with self.session_scope() as session:
            setting = session.query(AgentSettings).filter_by(key=key).first()
            if setting:
                setting.value = value
                setting.updated_at = datetime.now(timezone.utc)
            else:
                setting = AgentSettings(key=key, value=value)
                session.add(setting)
            session.commit()
            _log("info", "Setting updated", key=key, value=value)

    def get_all_settings(self) -> Dict[str, str]:
        """Get all settings as a dictionary."""
        with self.session_scope() as session:
            settings = session.query(AgentSettings).all()
            return {s.key: s.value for s in settings}

    # ============================= Scheduled Jobs =================================

    def create_scheduled_job(self, job_data: Dict[str, Any]) -> int:
        """Create a new scheduled job."""
        with self.session_scope() as session:
            job = ScheduledJob(
                name=job_data['name'],
                description=job_data.get('description'),
                enabled=job_data.get('enabled', True),
                schedule_type=job_data['schedule_type'],
                interval_seconds=job_data.get('interval_seconds'),
                cron_expr=job_data.get('cron_expr'),
                cron_tz=job_data.get('cron_tz'),
                run_at=job_data.get('run_at'),
                job_type=job_data['job_type'],
                custom_prompt=job_data.get('custom_prompt'),
                host_id=job_data.get('host_id'),
                next_run_at=job_data.get('next_run_at')
            )
            session.add(job)
            session.flush()
            job_id = job.id
            _log("info", "Scheduled job created", job_id=job_id, name=job_data['name'])
            return job_id

    def update_scheduled_job(self, job_id: int, updates: Dict[str, Any]) -> bool:
        """Update a scheduled job."""
        with self.session_scope() as session:
            job = session.query(ScheduledJob).filter_by(id=job_id).first()
            if not job:
                return False
            for key, value in updates.items():
                if hasattr(job, key):
                    setattr(job, key, value)
            job.updated_at = datetime.now(timezone.utc)
            _log("info", "Scheduled job updated", job_id=job_id)
            return True

    def delete_scheduled_job(self, job_id: int) -> bool:
        """Delete a scheduled job."""
        with self.session_scope() as session:
            job = session.query(ScheduledJob).filter_by(id=job_id).first()
            if not job:
                return False
            session.delete(job)
            _log("info", "Scheduled job deleted", job_id=job_id)
            return True

    def get_scheduled_job(self, job_id: int) -> Optional[Dict[str, Any]]:
        """Get a scheduled job by ID."""
        with self.session_scope() as session:
            job = session.query(ScheduledJob).filter_by(id=job_id).first()
            if not job:
                return None
            return self._job_to_dict(job)

    def list_scheduled_jobs(self, enabled_only: bool = False, host_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """List all scheduled jobs."""
        with self.session_scope() as session:
            query = session.query(ScheduledJob)
            if enabled_only:
                query = query.filter_by(enabled=True)
            if host_id:
                query = query.filter((ScheduledJob.host_id == host_id) | (ScheduledJob.host_id == None))
            jobs = query.order_by(ScheduledJob.next_run_at.asc().nullslast()).all()
            return [self._job_to_dict(j) for j in jobs]

    def get_due_jobs(self, host_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get jobs that are due to run (next_run_at <= now)."""
        now = datetime.now(timezone.utc)
        with self.session_scope() as session:
            query = session.query(ScheduledJob).filter(
                ScheduledJob.enabled == True,
                ScheduledJob.next_run_at <= now,
                ScheduledJob.last_status != 'running'
            )
            if host_id:
                query = query.filter((ScheduledJob.host_id == host_id) | (ScheduledJob.host_id == None))
            jobs = query.order_by(ScheduledJob.next_run_at.asc()).all()
            return [self._job_to_dict(j) for j in jobs]

    def mark_job_running(self, job_id: int) -> bool:
        """Mark a job as currently running."""
        with self.session_scope() as session:
            job = session.query(ScheduledJob).filter_by(id=job_id).first()
            if not job:
                return False
            job.last_status = 'running'
            return True

    def mark_job_completed(self, job_id: int, status: str, error: Optional[str] = None, duration_seconds: Optional[float] = None, next_run_at: Optional[datetime] = None) -> bool:
        """Mark a job as completed and update next run time."""
        with self.session_scope() as session:
            job = session.query(ScheduledJob).filter_by(id=job_id).first()
            if not job:
                return False
            job.last_run_at = datetime.now(timezone.utc)
            job.last_status = status
            job.last_error = error
            job.last_duration_seconds = duration_seconds
            job.run_count = (job.run_count or 0) + 1
            if next_run_at:
                job.next_run_at = next_run_at
            elif job.schedule_type == 'once':
                job.enabled = False  # Disable one-shot jobs after running
            _log("info", "Scheduled job completed", job_id=job_id, status=status)
            return True

    def _job_to_dict(self, job: 'ScheduledJob') -> Dict[str, Any]:
        """Convert a ScheduledJob to a dictionary."""
        return {
            'id': job.id,
            'name': job.name,
            'description': job.description,
            'enabled': job.enabled,
            'created_at': job.created_at.isoformat() if job.created_at else None,
            'updated_at': job.updated_at.isoformat() if job.updated_at else None,
            'schedule_type': job.schedule_type,
            'interval_seconds': job.interval_seconds,
            'cron_expr': job.cron_expr,
            'cron_tz': job.cron_tz,
            'run_at': job.run_at.isoformat() if job.run_at else None,
            'job_type': job.job_type,
            'custom_prompt': job.custom_prompt,
            'host_id': job.host_id,
            'next_run_at': job.next_run_at.isoformat() if job.next_run_at else None,
            'last_run_at': job.last_run_at.isoformat() if job.last_run_at else None,
            'last_status': job.last_status,
            'last_error': job.last_error,
            'last_duration_seconds': job.last_duration_seconds,
            'run_count': job.run_count
        }

    def count_recent_issues(self, hours: int = 24) -> int:
        """
        Count investigations with non-resolved outcomes in last N hours.
        Used for adaptive monitoring frequency - more issues = more frequent checks.

        Args:
            hours: How far back to look (default 24 hours)

        Returns:
            Count of investigations with outcomes: escalated, failed, monitoring
        """
        from datetime import timedelta
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        with self.session_scope() as session:
            return session.query(Investigation).filter(
                Investigation.started_at >= cutoff,
                Investigation.outcome.in_(['escalated', 'failed', 'monitoring'])
            ).count()

    # ============================= Investigation Learnings ======================

    def store_learning(self, learning_data: Dict[str, Any]) -> int:
        """
        Store a new learning extracted from an investigation.

        Args:
            learning_data: Dict with required fields:
                - learning_type: 'pattern', 'solution', 'root_cause', 'antipattern', 'insight'
                - title: Brief description
                - description: Detailed explanation
                Optional:
                - investigation_id: Source investigation
                - host_id: Which host this applies to
                - applies_when: Conditions when applicable
                - solution_steps: For solution type, ordered steps
                - services: List of services
                - tags: List of tags
                - category: High-level category

        Returns:
            Learning ID
        """
        import hashlib
        with self.session_scope() as session:
            # Build search text for FTS
            search_parts = [
                learning_data.get('title', ''),
                learning_data.get('description', ''),
                learning_data.get('applies_when', ''),
                learning_data.get('category', ''),
            ]
            services = learning_data.get('services', [])
            if services:
                search_parts.extend(services if isinstance(services, list) else [services])
            tags = learning_data.get('tags', [])
            if tags:
                search_parts.extend(tags if isinstance(tags, list) else [tags])
            search_text = ' '.join(str(p) for p in search_parts if p)

            # Hash for deduplication
            embedding_hash = hashlib.md5(search_text.encode()).hexdigest()

            learning = InvestigationLearning(
                investigation_id=learning_data.get('investigation_id'),
                host_id=learning_data.get('host_id', self.host_id),
                learning_type=learning_data['learning_type'],
                title=learning_data['title'],
                description=learning_data['description'],
                applies_when=learning_data.get('applies_when'),
                solution_steps=learning_data.get('solution_steps'),
                services=learning_data.get('services'),
                tags=learning_data.get('tags'),
                category=learning_data.get('category'),
                search_text=search_text,
                embedding_hash=embedding_hash,
            )
            session.add(learning)
            session.flush()
            learning_id = learning.id
            _log("info", "Learning stored",
                 learning_id=learning_id,
                 learning_type=learning_data['learning_type'],
                 title=learning_data['title'][:50])
            return learning_id

    def get_learning(self, learning_id: int) -> Optional[Dict[str, Any]]:
        """Get a learning by ID."""
        with self.session_scope() as session:
            learning = session.query(InvestigationLearning).filter_by(id=learning_id).first()
            if not learning:
                return None
            return self._learning_to_dict(learning)

    def find_learnings(
        self,
        query: Optional[str] = None,
        learning_type: Optional[str] = None,
        services: Optional[List[str]] = None,
        tags: Optional[List[str]] = None,
        category: Optional[str] = None,
        verified_only: bool = False,
        limit: int = 20
    ) -> List[Dict[str, Any]]:
        """
        Find learnings by various criteria with optional full-text search.

        Args:
            query: Text search query (optional)
            learning_type: Filter by type
            services: Filter by services (any match)
            tags: Filter by tags (any match)
            category: Filter by category
            verified_only: Only return verified learnings
            limit: Maximum results

        Returns:
            List of matching learnings
        """
        with self.session_scope() as session:
            # Build base query
            q = session.query(InvestigationLearning).filter(
                InvestigationLearning.deprecated == False
            )

            if verified_only:
                q = q.filter(InvestigationLearning.verified == True)

            if learning_type:
                q = q.filter(InvestigationLearning.learning_type == learning_type)

            if category:
                q = q.filter(InvestigationLearning.category == category)

            # Service filter using JSONB containment
            if services:
                # Check if services array contains any of the requested services
                q = q.filter(
                    InvestigationLearning.services.op('?|')(services)
                )

            # Tag filter using JSONB containment
            if tags:
                q = q.filter(
                    InvestigationLearning.tags.op('?|')(tags)
                )

            # Full-text search on search_text if query provided
            if query:
                q = q.filter(
                    func.to_tsvector('english', InvestigationLearning.search_text).op('@@')(
                        func.plainto_tsquery('english', query)
                    )
                )
                # Order by FTS rank
                q = q.order_by(
                    func.ts_rank_cd(
                        func.to_tsvector('english', InvestigationLearning.search_text),
                        func.plainto_tsquery('english', query)
                    ).desc()
                )
            else:
                # Default order by success rate and recency
                q = q.order_by(
                    InvestigationLearning.success_rate.desc().nullslast(),
                    InvestigationLearning.created_at.desc()
                )

            results = q.limit(limit).all()
            return [self._learning_to_dict(l) for l in results]

    def find_learnings_hybrid(
        self,
        query_text: str,
        query_embedding: Optional[List[float]] = None,
        limit: int = 5,
        vector_weight: float = 0.7
    ) -> List[Dict[str, Any]]:
        """
        Find learnings using hybrid search (vector similarity + FTS).

        Falls back to FTS-only if no embedding provided.
        """
        from sqlalchemy import text
        try:
            with self.session_scope() as session:
                if query_embedding and len(query_embedding) > 0:
                    embedding_str = "[" + ",".join(str(v) for v in query_embedding) + "]"
                    fts_weight = 1.0 - vector_weight

                    result = session.execute(text(f"""
                        WITH vector_scores AS (
                            SELECT le.learning_id,
                                1 - (le.embedding <=> '{embedding_str}'::vector) as vector_sim
                            FROM learning_embeddings le
                        ),
                        fts_scores AS (
                            SELECT il.id as learning_id,
                                ts_rank_cd(to_tsvector('english', il.search_text),
                                           plainto_tsquery('english', :query)) as fts_rank
                            FROM investigation_learnings il
                            WHERE il.deprecated = false
                              AND to_tsvector('english', il.search_text) @@ plainto_tsquery('english', :query)
                        )
                        SELECT il.id, il.learning_type, il.title, il.description,
                               il.applies_when, il.services, il.tags, il.category,
                               il.success_rate, il.created_at,
                               COALESCE(v.vector_sim, 0) as vector_sim,
                               COALESCE(f.fts_rank, 0) as fts_rank,
                               (COALESCE(v.vector_sim, 0) * :vw +
                                COALESCE(f.fts_rank, 0) * :fw) as combined_score
                        FROM investigation_learnings il
                        LEFT JOIN vector_scores v ON il.id = v.learning_id
                        LEFT JOIN fts_scores f ON il.id = f.learning_id
                        WHERE il.deprecated = false
                          AND (v.vector_sim IS NOT NULL OR f.fts_rank IS NOT NULL)
                        ORDER BY combined_score * COALESCE(
                            CASE WHEN il.times_applied >= 3 THEN 0.5 + COALESCE(il.success_rate, 0.5)
                                 ELSE 1.0 END,
                            1.0) DESC
                        LIMIT :limit
                    """), {
                        'query': query_text,
                        'vw': vector_weight,
                        'fw': fts_weight,
                        'limit': limit
                    })
                else:
                    # FTS-only fallback
                    return self.find_learnings(query=query_text, limit=limit)

                rows = result.fetchall()
                return [
                    {
                        'id': row[0],
                        'learning_type': row[1],
                        'title': row[2],
                        'description': row[3],
                        'applies_when': row[4] or '',
                        'services': row[5] or [],
                        'tags': row[6] or [],
                        'category': row[7] or '',
                        'success_rate': float(row[8]) if row[8] else None,
                        'vector_similarity': round(float(row[10]), 3) if row[10] else 0,
                        'fts_rank': round(float(row[11]), 3) if row[11] else 0,
                    }
                    for row in rows
                ]
        except Exception as e:
            _log("warn", "Hybrid learning search failed, falling back to FTS", error=str(e))
            return self.find_learnings(query=query_text, limit=limit)

    def record_learning_application(self, learning_id: int, successful: bool) -> bool:
        """
        Record that a learning was applied and whether it was successful.

        This updates the effectiveness tracking for the learning.

        Args:
            learning_id: ID of the learning that was applied
            successful: Whether the application was successful

        Returns:
            True if updated successfully
        """
        with self.session_scope() as session:
            learning = session.query(InvestigationLearning).filter_by(id=learning_id).first()
            if not learning:
                return False

            learning.times_applied = (learning.times_applied or 0) + 1
            if successful:
                learning.times_successful = (learning.times_successful or 0) + 1

            # Update success rate
            if learning.times_applied > 0:
                learning.success_rate = learning.times_successful / learning.times_applied

            _log("info", "Learning application recorded",
                 learning_id=learning_id,
                 successful=successful,
                 times_applied=learning.times_applied,
                 success_rate=learning.success_rate)
            return True

    def verify_learning(self, learning_id: int, verified: bool = True) -> bool:
        """Mark a learning as verified or not."""
        with self.session_scope() as session:
            learning = session.query(InvestigationLearning).filter_by(id=learning_id).first()
            if not learning:
                return False
            learning.verified = verified
            return True

    def deprecate_learning(self, learning_id: int, deprecated: bool = True) -> bool:
        """Mark a learning as deprecated."""
        with self.session_scope() as session:
            learning = session.query(InvestigationLearning).filter_by(id=learning_id).first()
            if not learning:
                return False
            learning.deprecated = deprecated
            return True

    def get_learnings_for_services(self, services: List[str], limit: int = 10) -> List[Dict[str, Any]]:
        """
        Get top learnings relevant to specific services.

        Ordered by success rate for quick lookup during investigations.

        Args:
            services: List of service names
            limit: Maximum results

        Returns:
            List of learnings
        """
        return self.find_learnings(services=services, limit=limit)

    def get_learnings_since(self, since, limit: int = 50) -> List[Dict[str, Any]]:
        """Get all learnings created since a given timestamp."""
        with self.session_scope() as session:
            learnings = session.query(InvestigationLearning).filter(
                InvestigationLearning.created_at >= since,
                InvestigationLearning.deprecated == False
            ).order_by(InvestigationLearning.created_at.desc()).limit(limit).all()
            return [self._learning_to_dict(l) for l in learnings]

    def _learning_to_dict(self, learning: 'InvestigationLearning') -> Dict[str, Any]:
        """Convert an InvestigationLearning to a dictionary."""
        return {
            'id': learning.id,
            'created_at': learning.created_at.isoformat() if learning.created_at else None,
            'updated_at': learning.updated_at.isoformat() if learning.updated_at else None,
            'investigation_id': learning.investigation_id,
            'host_id': learning.host_id,
            'learning_type': learning.learning_type,
            'title': learning.title,
            'description': learning.description,
            'applies_when': learning.applies_when,
            'solution_steps': learning.solution_steps,
            'services': learning.services,
            'tags': learning.tags,
            'category': learning.category,
            'times_applied': learning.times_applied,
            'times_successful': learning.times_successful,
            'success_rate': learning.success_rate,
            'verified': learning.verified,
            'deprecated': learning.deprecated,
        }

    # ============================= Statistics =================================

    def get_stats(self) -> Dict[str, Any]:
        """Get knowledge base statistics."""
        with self.session_scope() as session:
            return {
                "enabled": True,
                "db_type": "postgresql",
                "system_profiles_count": session.query(SystemProfile).count(),
                "has_system_purpose": session.query(SystemPurpose).count() > 0,
                "baselines_count": session.query(BaselineState).count(),
                "drift_events_count": session.query(DriftEvent).count(),
                "investigations_count": session.query(Investigation).count(),
                "uninvestigated_drift_count": session.query(DriftEvent).filter_by(investigated=False).count()
            }

    # ============================= Investigation Priority Helpers =====================

    def get_trigger_occurrences(self, trigger: str, hours: int = 1) -> int:
        """
        Count how many times a similar trigger occurred in the last N hours.

        Uses fuzzy matching on trigger text to find similar issues.

        Args:
            trigger: The trigger text to match
            hours: How far back to look (default 1 hour)

        Returns:
            Count of similar investigations
        """
        from datetime import timedelta
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

        # Extract key terms for matching (lowercase, remove noise)
        trigger_lower = trigger.lower().strip()

        with self.session_scope() as session:
            # Use PostgreSQL ILIKE for case-insensitive partial matching
            # Match investigations where trigger contains key terms
            count = session.query(Investigation).filter(
                Investigation.started_at >= cutoff,
                func.lower(Investigation.trigger).contains(trigger_lower[:50])  # First 50 chars
            ).count()

            # If no exact match, try to match on service name patterns
            if count == 0:
                # Extract potential service name (common patterns)
                import re
                service_match = re.search(r'(\w[\w-]{2,})', trigger_lower)
                if service_match:
                    service_name = service_match.group(1)
                    count = session.query(Investigation).filter(
                        Investigation.started_at >= cutoff,
                        func.lower(Investigation.trigger).contains(service_name)
                    ).count()

            return count

    def get_escalation_count(self, trigger: str, days: int = 7) -> int:
        """
        Count how many times similar issues were escalated in the last N days.

        Args:
            trigger: The trigger text to match
            days: How far back to look (default 7 days)

        Returns:
            Count of escalated investigations with similar triggers
        """
        from datetime import timedelta
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

        trigger_lower = trigger.lower().strip()

        with self.session_scope() as session:
            return session.query(Investigation).filter(
                Investigation.started_at >= cutoff,
                Investigation.outcome == 'escalated',
                func.lower(Investigation.trigger).contains(trigger_lower[:50])
            ).count()

    def get_failed_remediation_count(self, trigger: str, days: int = 7) -> int:
        """
        Count failed remediations for similar issues.

        Args:
            trigger: The trigger text to match
            days: How far back to look (default 7 days)

        Returns:
            Count of failed investigations with similar triggers
        """
        from datetime import timedelta
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

        trigger_lower = trigger.lower().strip()

        with self.session_scope() as session:
            return session.query(Investigation).filter(
                Investigation.started_at >= cutoff,
                Investigation.outcome == 'failed',
                func.lower(Investigation.trigger).contains(trigger_lower[:50])
            ).count()

    def get_last_investigation_time(self, trigger: str) -> Optional[datetime]:
        """
        Get the time of the most recent investigation for a similar issue.

        Args:
            trigger: The trigger text to match

        Returns:
            Datetime of last investigation, or None if never investigated
        """
        trigger_lower = trigger.lower().strip()

        with self.session_scope() as session:
            result = session.query(Investigation.started_at).filter(
                func.lower(Investigation.trigger).contains(trigger_lower[:50])
            ).order_by(Investigation.started_at.desc()).first()

            return result[0] if result else None

    def get_affected_services_from_correlations(self, service_name: str) -> List[str]:
        """
        Get services that are correlated with the given service.

        Uses ServiceCorrelation table to find related services.

        Args:
            service_name: The primary service name

        Returns:
            List of correlated service names
        """
        with self.session_scope() as session:
            # Get services where this is service_a
            correlations_a = session.query(ServiceCorrelation.service_b).filter(
                ServiceCorrelation.service_a == service_name
            ).all()

            # Get services where this is service_b
            correlations_b = session.query(ServiceCorrelation.service_a).filter(
                ServiceCorrelation.service_b == service_name
            ).all()

            services = set()
            for (svc,) in correlations_a:
                services.add(svc)
            for (svc,) in correlations_b:
                services.add(svc)

            return list(services)

    def calculate_investigation_priority(self, trigger: str, operator_flagged: bool = False) -> int:
        """
        Calculate dynamic priority for an investigation based on multiple factors.

        Priority ranges from 1 (highest) to 10 (lowest).

        Args:
            trigger: The trigger text that prompted investigation
            operator_flagged: Whether operator marked this as urgent

        Returns:
            Priority value 1-10
        """
        from agent.priority import calculate_priority_from_trigger

        # Get historical data
        occurrences_1h = self.get_trigger_occurrences(trigger, hours=1)
        occurrences_24h = self.get_trigger_occurrences(trigger, hours=24)
        escalations = self.get_escalation_count(trigger, days=7)
        failed_remediations = self.get_failed_remediation_count(trigger, days=7)

        # Calculate time since last investigation
        last_time = self.get_last_investigation_time(trigger)
        if last_time:
            minutes_since = (datetime.now(timezone.utc) - last_time).total_seconds() / 60
        else:
            minutes_since = float('inf')

        # Get affected services if we can identify the service
        import re
        service_match = re.search(r'(\w[\w-]{2,})', trigger.lower())
        affected_services = None
        if service_match:
            service_name = service_match.group(1)
            affected_services = self.get_affected_services_from_correlations(service_name)

        # Calculate priority
        result = calculate_priority_from_trigger(
            trigger=trigger,
            occurrences_last_hour=occurrences_1h,
            occurrences_last_24h=occurrences_24h,
            previous_escalations=escalations,
            failed_remediations=failed_remediations,
            operator_flagged=operator_flagged,
            affected_services=affected_services,
            minutes_since_last_investigation=minutes_since,
        )

        _log("debug", "Calculated investigation priority",
             trigger=trigger[:100],
             priority=result.priority,
             reasoning=result.reasoning,
             severity_score=result.severity_score,
             impact_score=result.impact_score,
             history_score=result.history_score,
             time_score=result.time_score)

        return result.priority

    # ============================= Investigation Queue =================================

    def queue_investigation(
        self,
        queue_type: str,
        investigation_id: Optional[int] = None,
        target_service: Optional[str] = None,
        operator_context: Optional[str] = None,
        priority: Optional[int] = None,
        trigger: Optional[str] = None,
        auto_priority: bool = True,
        operator_flagged: bool = False
    ) -> int:
        """
        Add an investigation to the queue (from triage actions).

        Args:
            queue_type: Type of queue entry ('retry', 'context', 'service_check')
            investigation_id: Parent investigation ID if retriggering
            target_service: Specific service to investigate
            operator_context: Notes/context from operator
            priority: Explicit priority (1-10, lower = higher priority).
                     If None and auto_priority=True, calculated dynamically.
            trigger: Trigger text for auto-priority calculation
            auto_priority: If True, calculate priority from trigger/context
            operator_flagged: If True, boosts priority for urgent issues

        Returns:
            Queue ID
        """
        # Calculate priority if not explicitly provided
        if priority is None and auto_priority:
            # Build trigger text from available context
            if trigger:
                priority_trigger = trigger
            elif operator_context:
                priority_trigger = operator_context
            elif target_service:
                priority_trigger = f"service check for {target_service}"
            else:
                priority_trigger = queue_type

            priority = self.calculate_investigation_priority(
                trigger=priority_trigger,
                operator_flagged=operator_flagged
            )
        elif priority is None:
            priority = 5  # Default mid-priority

        with self.session_scope() as session:
            queue_item = InvestigationQueue(
                investigation_id=investigation_id,
                queue_type=queue_type,
                target_service=target_service,
                operator_context=operator_context,
                priority=priority,
                status='pending'
            )
            session.add(queue_item)
            session.flush()
            queue_id = queue_item.id
            _log("info", "Investigation queued",
                 queue_id=queue_id, type=queue_type, priority=priority,
                 auto_calculated=auto_priority and trigger is not None)
            return queue_id

    def get_next_queued_investigation(self) -> Optional[Dict[str, Any]]:
        """Get the next pending investigation from the queue (highest priority first)."""
        with self.session_scope() as session:
            item = session.query(InvestigationQueue).filter_by(
                status='pending'
            ).order_by(
                InvestigationQueue.priority.asc(),
                InvestigationQueue.created_at.asc()
            ).first()

            if item:
                # Mark as processing
                item.status = 'processing'
                session.flush()
                return {
                    "id": item.id,
                    "queue_type": item.queue_type,
                    "investigation_id": item.investigation_id,
                    "target_service": item.target_service,
                    "operator_context": item.operator_context,
                    "priority": item.priority,
                    "created_at": item.created_at.isoformat() if item.created_at else None
                }
            return None

    def complete_queued_investigation(self, queue_id: int, result_investigation_id: int) -> None:
        """Mark a queued investigation as completed with result."""
        with self.session_scope() as session:
            item = session.query(InvestigationQueue).filter_by(id=queue_id).first()
            if item:
                item.status = 'completed'
                item.processed_at = datetime.now(timezone.utc)
                item.result_investigation_id = result_investigation_id
                _log("info", "Queue item completed",
                     queue_id=queue_id, result_investigation_id=result_investigation_id)

    def cancel_queued_investigation(self, queue_id: int) -> bool:
        """Cancel a queued investigation."""
        with self.session_scope() as session:
            item = session.query(InvestigationQueue).filter_by(id=queue_id, status='pending').first()
            if item:
                item.status = 'cancelled'
                _log("info", "Queue item cancelled", queue_id=queue_id)
                return True
            return False

    def get_queue_wait_time_seconds(self, queue_id: int) -> Optional[float]:
        """Get the time a queue item waited before processing (seconds)."""
        with self.session_scope() as session:
            item = session.query(InvestigationQueue).filter_by(id=queue_id).first()
            if item and item.created_at and item.processed_at:
                return (item.processed_at - item.created_at).total_seconds()
            return None

    def get_investigation_queue(self, include_completed: bool = False, limit: int = 50) -> List[Dict[str, Any]]:
        """Get the investigation queue."""
        with self.session_scope() as session:
            query = session.query(InvestigationQueue)
            if not include_completed:
                query = query.filter(InvestigationQueue.status.in_(['pending', 'processing']))
            query = query.order_by(
                InvestigationQueue.status.asc(),  # pending first
                InvestigationQueue.priority.asc(),
                InvestigationQueue.created_at.desc()
            ).limit(limit)

            return [
                {
                    "id": item.id,
                    "created_at": item.created_at.isoformat() if item.created_at else None,
                    "investigation_id": item.investigation_id,
                    "queue_type": item.queue_type,
                    "target_service": item.target_service,
                    "operator_context": item.operator_context,
                    "priority": item.priority,
                    "status": item.status,
                    "processed_at": item.processed_at.isoformat() if item.processed_at else None,
                    "result_investigation_id": item.result_investigation_id
                }
                for item in query.all()
            ]

    def get_investigation(self, investigation_id: int) -> Optional[Dict[str, Any]]:
        """Get a single investigation by ID."""
        with self.session_scope() as session:
            inv = session.query(Investigation).filter_by(id=investigation_id).first()
            if inv:
                return {
                    "id": inv.id,
                    "started_at": inv.started_at.isoformat() if inv.started_at else None,
                    "completed_at": inv.completed_at.isoformat() if inv.completed_at else None,
                    "trigger": inv.trigger,
                    "findings": inv.findings,
                    "outcome": inv.outcome,
                    "duration_seconds": inv.duration_seconds,
                    "tool_calls_count": inv.tool_calls_count,
                    "parent_investigation_id": inv.parent_investigation_id,
                    "operator_notes": inv.operator_notes,
                    "triage_action": inv.triage_action,
                    "host_id": inv.host_id
                }
            return None

    def update_investigation_triage(
        self,
        investigation_id: int,
        triage_action: str,
        operator_notes: Optional[str] = None,
        outcome: Optional[str] = None
    ) -> bool:
        """Update an investigation with triage action."""
        with self.session_scope() as session:
            inv = session.query(Investigation).filter_by(id=investigation_id).first()
            if inv:
                inv.triage_action = triage_action
                if operator_notes:
                    inv.operator_notes = operator_notes
                if outcome:
                    inv.outcome = outcome
                _log("info", "Investigation triage updated",
                     investigation_id=investigation_id, action=triage_action)
                return True
            return False

    # ============================= Suppression Patterns =================================

    def add_suppression_pattern(
        self,
        service_name: str,
        trigger_pattern: str,
        reason: Optional[str] = None,
        investigation_id: Optional[int] = None,
        expires_hours: Optional[int] = None
    ) -> int:
        """Add a suppression pattern (from false positive triage)."""
        from datetime import timedelta
        with self.session_scope() as session:
            expires_at = None
            if expires_hours:
                expires_at = datetime.now(timezone.utc) + timedelta(hours=expires_hours)

            pattern = SuppressionPattern(
                service_name=service_name,
                trigger_pattern=trigger_pattern,
                reason=reason,
                created_from_investigation_id=investigation_id,
                expires_at=expires_at
            )
            session.add(pattern)
            session.flush()
            pattern_id = pattern.id
            _log("info", "Suppression pattern added",
                 pattern_id=pattern_id, service=service_name, pattern=trigger_pattern)
            return pattern_id

    def get_active_suppressions(self) -> List[Dict[str, Any]]:
        """Get all active suppression patterns (checking expiry)."""
        with self.session_scope() as session:
            now = datetime.now(timezone.utc)
            patterns = session.query(SuppressionPattern).filter(
                SuppressionPattern.active == True,
                (SuppressionPattern.expires_at.is_(None) | (SuppressionPattern.expires_at > now))
            ).all()

            return [
                {
                    "id": p.id,
                    "service_name": p.service_name,
                    "trigger_pattern": p.trigger_pattern,
                    "reason": p.reason,
                    "created_at": p.created_at.isoformat() if p.created_at else None,
                    "expires_at": p.expires_at.isoformat() if p.expires_at else None
                }
                for p in patterns
            ]

    def should_suppress(self, service: str, trigger: str) -> bool:
        """Check if this service+trigger combo should be suppressed."""
        patterns = self.get_active_suppressions()
        for p in patterns:
            if p["service_name"] == service and p["trigger_pattern"].lower() in trigger.lower():
                _log("debug", "Alert suppressed", service=service, pattern=p["trigger_pattern"])
                return True
        return False

    def deactivate_suppression(self, pattern_id: int) -> bool:
        """Deactivate a suppression pattern."""
        with self.session_scope() as session:
            pattern = session.query(SuppressionPattern).filter_by(id=pattern_id).first()
            if pattern:
                pattern.active = False
                _log("info", "Suppression pattern deactivated", pattern_id=pattern_id)
                return True
            return False

    def get_service_investigations(self, service_name: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Get recent investigations for a specific service."""
        with self.session_scope() as session:
            # Search in trigger or findings for the service name
            investigations = session.query(Investigation).filter(
                (Investigation.trigger.ilike(f'%{service_name}%')) |
                (cast(Investigation.findings, SQLText).ilike(f'%{service_name}%'))
            ).order_by(Investigation.started_at.desc()).limit(limit).all()

            return [
                {
                    "id": inv.id,
                    "started_at": inv.started_at.isoformat() if inv.started_at else None,
                    "trigger": inv.trigger,
                    "outcome": inv.outcome,
                    "duration_seconds": inv.duration_seconds,
                    "triage_action": inv.triage_action
                }
                for inv in investigations
            ]

    def get_service_drift_events(self, service_name: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Get recent drift events for a specific service."""
        with self.session_scope() as session:
            events = session.query(DriftEvent).filter(
                (DriftEvent.description.ilike(f'%{service_name}%')) |
                (cast(DriftEvent.drift_details, SQLText).ilike(f'%{service_name}%'))
            ).order_by(DriftEvent.detected_at.desc()).limit(limit).all()

            return [
                {
                    "id": e.id,
                    "detected_at": e.detected_at.isoformat() if e.detected_at else None,
                    "drift_type": e.drift_type,
                    "description": e.description,
                    "investigated": e.investigated
                }
                for e in events
            ]

    # ========================= Config Changes (Phase 5) ==========================

    def propose_config_change(
        self,
        config_type: str,
        target_file: str,
        change_type: str,
        service: str,
        description: str,
        change_spec: Dict[str, Any],
        reason: str,
        proposed_diff: Optional[str] = None,
        investigation_id: Optional[int] = None
    ) -> int:
        """Create a new config change proposal. Returns the change ID."""
        import hashlib

        # Generate dedup key to prevent duplicate proposals
        dedup_content = (
            f"{config_type or ''}::"
            f"{target_file or ''}::"
            f"{change_type or ''}::"
            f"{service or ''}::"
            f"{json.dumps(change_spec, sort_keys=True)}"
        )
        dedup_key = hashlib.md5(dedup_content.encode()).hexdigest()

        with self.session_scope() as session:
            # Check for existing proposed change with same dedup_key
            existing = session.query(ConfigChange).filter(
                ConfigChange.dedup_key == dedup_key,
                ConfigChange.status == 'proposed'
            ).first()

            if existing:
                _log("info", "Config change already proposed (duplicate prevented)",
                     change_id=existing.id, dedup_key=dedup_key, service=service)
                return existing.id

            change = ConfigChange(
                config_type=config_type,
                target_file=target_file,
                change_type=change_type,
                service=service,
                description=description,
                change_spec=change_spec,
                reason=reason,
                proposed_diff=proposed_diff,
                status='proposed',
                investigation_id=investigation_id,
                dedup_key=dedup_key
            )
            session.add(change)
            session.flush()
            change_id = change.id
            _log("info", "Config change proposed", change_id=change_id, config_type=config_type, service=service, dedup_key=dedup_key)
            return change_id

    def get_config_change(self, change_id: int) -> Optional[Dict[str, Any]]:
        """Get a specific config change by ID."""
        with self.session_scope() as session:
            change = session.query(ConfigChange).filter(ConfigChange.id == change_id).first()
            if not change:
                return None
            return self._config_change_to_dict(change)

    def get_pending_config_changes(self) -> List[Dict[str, Any]]:
        """Get all pending (proposed) config changes."""
        with self.session_scope() as session:
            changes = session.query(ConfigChange).filter(
                ConfigChange.status == 'proposed'
            ).order_by(ConfigChange.created_at.desc()).all()
            return [self._config_change_to_dict(c) for c in changes]

    def get_all_config_changes(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Get all config changes (for history)."""
        with self.session_scope() as session:
            changes = session.query(ConfigChange).order_by(
                ConfigChange.created_at.desc()
            ).limit(limit).all()
            return [self._config_change_to_dict(c) for c in changes]

    def approve_config_change(self, change_id: int, approved_by: str = 'user') -> bool:
        """Approve a pending config change."""
        with self.session_scope() as session:
            change = session.query(ConfigChange).filter(ConfigChange.id == change_id).first()
            if not change or change.status != 'proposed':
                return False
            change.status = 'approved'
            change.applied_by = approved_by
            _log("info", "Config change approved", change_id=change_id, approved_by=approved_by)
            return True

    def reject_config_change(self, change_id: int, reason: Optional[str] = None) -> bool:
        """Reject a pending config change."""
        with self.session_scope() as session:
            change = session.query(ConfigChange).filter(ConfigChange.id == change_id).first()
            if not change or change.status != 'proposed':
                return False
            change.status = 'rejected'
            if reason:
                change.verification_result = {"rejection_reason": reason}
            _log("info", "Config change rejected", change_id=change_id)
            return True

    def mark_config_applied(
        self,
        change_id: int,
        backup_path: Optional[str] = None,
        original_content: Optional[str] = None,
        verification_result: Optional[Dict[str, Any]] = None
    ) -> bool:
        """Mark a config change as applied."""
        with self.session_scope() as session:
            change = session.query(ConfigChange).filter(ConfigChange.id == change_id).first()
            if not change:
                return False
            change.status = 'applied'
            change.applied_at = datetime.now(timezone.utc)
            if backup_path:
                change.backup_path = backup_path
            if original_content:
                change.original_content = original_content
            if verification_result:
                change.verification_result = verification_result
            _log("info", "Config change applied", change_id=change_id, backup_path=backup_path)
            return True

    def mark_config_failed(self, change_id: int, error: str) -> bool:
        """Mark a config change as failed."""
        with self.session_scope() as session:
            change = session.query(ConfigChange).filter(ConfigChange.id == change_id).first()
            if not change:
                return False
            change.status = 'failed'
            change.verification_result = {"error": error}
            _log("error", "Config change failed", change_id=change_id, error=error)
            return True

    def rollback_config_change(self, change_id: int) -> Optional[str]:
        """Mark a config change as rolled back. Returns the original content for restoration."""
        with self.session_scope() as session:
            change = session.query(ConfigChange).filter(ConfigChange.id == change_id).first()
            if not change or change.status != 'applied':
                return None
            original = change.original_content
            change.status = 'rolled_back'
            _log("info", "Config change rolled back", change_id=change_id)
            return original

    def _config_change_to_dict(self, change: ConfigChange) -> Dict[str, Any]:
        """Convert ConfigChange model to dictionary."""
        return {
            "id": change.id,
            "created_at": change.created_at.isoformat() if change.created_at else None,
            "config_type": change.config_type,
            "target_file": change.target_file,
            "change_type": change.change_type,
            "service": change.service,
            "description": change.description,
            "change_spec": change.change_spec,
            "reason": change.reason,
            "proposed_diff": change.proposed_diff,
            "status": change.status,
            "applied_at": change.applied_at.isoformat() if change.applied_at else None,
            "applied_by": change.applied_by,
            "verification_result": change.verification_result,
            "backup_path": change.backup_path,
            "investigation_id": change.investigation_id
        }

    # ========================= Notification Methods =============================

    def get_notification_channels(self, enabled_only: bool = False) -> List[Dict[str, Any]]:
        """Get all notification channels."""
        with self.session_scope() as session:
            query = session.query(NotificationChannel)
            if enabled_only:
                query = query.filter(NotificationChannel.enabled == True)
            channels = query.order_by(NotificationChannel.channel_type).all()
            return [self._channel_to_dict(c) for c in channels]

    def get_notification_channel(self, channel_id: int) -> Optional[Dict[str, Any]]:
        """Get a specific notification channel by ID."""
        with self.session_scope() as session:
            channel = session.query(NotificationChannel).filter(NotificationChannel.id == channel_id).first()
            return self._channel_to_dict(channel) if channel else None

    def create_notification_channel(
        self,
        channel_type: str,
        name: str,
        config: Dict[str, Any],
        severity_filter: str = 'warning',
        enabled: bool = True
    ) -> int:
        """Create a new notification channel."""
        with self.session_scope() as session:
            channel = NotificationChannel(
                channel_type=channel_type,
                name=name,
                config=config,
                severity_filter=severity_filter,
                enabled=enabled
            )
            session.add(channel)
            session.flush()
            channel_id = channel.id
            _log("info", "Notification channel created", channel_id=channel_id, channel_type=channel_type, name=name)
            return channel_id

    def update_notification_channel(
        self,
        channel_id: int,
        name: Optional[str] = None,
        config: Optional[Dict[str, Any]] = None,
        severity_filter: Optional[str] = None,
        enabled: Optional[bool] = None
    ) -> bool:
        """Update a notification channel."""
        with self.session_scope() as session:
            channel = session.query(NotificationChannel).filter(NotificationChannel.id == channel_id).first()
            if not channel:
                return False
            if name is not None:
                channel.name = name
            if config is not None:
                channel.config = config
            if severity_filter is not None:
                channel.severity_filter = severity_filter
            if enabled is not None:
                channel.enabled = enabled
            _log("info", "Notification channel updated", channel_id=channel_id)
            return True

    def delete_notification_channel(self, channel_id: int) -> bool:
        """Delete a notification channel."""
        with self.session_scope() as session:
            channel = session.query(NotificationChannel).filter(NotificationChannel.id == channel_id).first()
            if not channel:
                return False
            session.delete(channel)
            _log("info", "Notification channel deleted", channel_id=channel_id)
            return True

    def record_notification_result(
        self,
        channel_id: int,
        success: bool,
        error_message: Optional[str] = None
    ) -> None:
        """Update channel with last notification result."""
        with self.session_scope() as session:
            channel = session.query(NotificationChannel).filter(NotificationChannel.id == channel_id).first()
            if channel:
                now = datetime.now(timezone.utc)
                if success:
                    channel.last_success_at = now
                    channel.failure_count = 0
                else:
                    channel.last_failure_at = now
                    channel.failure_count = (channel.failure_count or 0) + 1

    def record_notification_history(
        self,
        channel_id: int,
        channel_type: str,
        severity: str,
        title: str,
        message: str,
        success: bool,
        context: Optional[Dict[str, Any]] = None,
        error_message: Optional[str] = None
    ) -> int:
        """Record a notification in history."""
        with self.session_scope() as session:
            history = NotificationHistory(
                channel_id=channel_id,
                channel_type=channel_type,
                severity=severity,
                title=title,
                message=message,
                context=context,
                success=success,
                error_message=error_message
            )
            session.add(history)
            session.flush()
            return history.id

    def get_notification_history(self, limit: int = 50, channel_id: Optional[int] = None) -> List[Dict[str, Any]]:
        """Get recent notification history."""
        with self.session_scope() as session:
            query = session.query(NotificationHistory)
            if channel_id:
                query = query.filter(NotificationHistory.channel_id == channel_id)
            history = query.order_by(NotificationHistory.sent_at.desc()).limit(limit).all()
            return [{
                "id": h.id,
                "sent_at": h.sent_at.isoformat() if h.sent_at else None,
                "channel_id": h.channel_id,
                "channel_type": h.channel_type,
                "severity": h.severity,
                "title": h.title,
                "message": h.message,
                "context": h.context,
                "success": h.success,
                "error_message": h.error_message
            } for h in history]

    def _channel_to_dict(self, channel: NotificationChannel) -> Dict[str, Any]:
        """Convert NotificationChannel model to dictionary."""
        return {
            "id": channel.id,
            "created_at": channel.created_at.isoformat() if channel.created_at else None,
            "updated_at": channel.updated_at.isoformat() if channel.updated_at else None,
            "channel_type": channel.channel_type,
            "name": channel.name,
            "enabled": channel.enabled,
            "config": channel.config,
            "severity_filter": channel.severity_filter,
            "last_success_at": channel.last_success_at.isoformat() if channel.last_success_at else None,
            "last_failure_at": channel.last_failure_at.isoformat() if channel.last_failure_at else None,
            "failure_count": channel.failure_count
        }

    # ============================= Correlation Analysis (Phase 6) =================

    def record_metric_snapshot(
        self,
        metrics: Dict[str, Any],
        snapshot_type: str = 'health_check',
        investigation_id: Optional[int] = None
    ) -> int:
        """Record a metric snapshot for correlation analysis."""
        with self.session_scope() as session:
            snapshot = MetricSnapshot(
                host_id=self.host_id,
                investigation_id=investigation_id,
                snapshot_type=snapshot_type,
                metrics=metrics
            )
            session.add(snapshot)
            session.flush()
            return snapshot.id

    def get_metric_snapshots(
        self,
        hours: int = 24,
        snapshot_type: Optional[str] = None,
        investigation_id: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """Get metric snapshots for a time window."""
        from datetime import timedelta
        with self.session_scope() as session:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
            query = session.query(MetricSnapshot).filter(
                MetricSnapshot.captured_at >= cutoff
            )
            if snapshot_type:
                query = query.filter(MetricSnapshot.snapshot_type == snapshot_type)
            if investigation_id:
                query = query.filter(MetricSnapshot.investigation_id == investigation_id)

            snapshots = query.order_by(MetricSnapshot.captured_at.desc()).all()
            return [{
                "id": s.id,
                "captured_at": s.captured_at.isoformat() if s.captured_at else None,
                "host_id": s.host_id,
                "investigation_id": s.investigation_id,
                "snapshot_type": s.snapshot_type,
                "metrics": s.metrics
            } for s in snapshots]

    def find_correlated_events(
        self,
        window_seconds: int = 300,
        hours: int = 24
    ) -> List[Dict[str, Any]]:
        """Find events (investigations + drift) that occurred within a time window of each other."""
        from datetime import timedelta
        with self.session_scope() as session:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

            # Get investigations and drift events in the time range
            investigations = session.query(Investigation).filter(
                Investigation.started_at >= cutoff
            ).all()

            drift_events = session.query(DriftEvent).filter(
                DriftEvent.detected_at >= cutoff
            ).all()

            correlations = []

            # Find investigation <-> drift correlations
            for inv in investigations:
                for drift in drift_events:
                    delta = abs((inv.started_at - drift.detected_at).total_seconds())
                    if delta <= window_seconds:
                        correlations.append({
                            "event_a": {
                                "type": "investigation",
                                "id": inv.id,
                                "time": inv.started_at.isoformat(),
                                "trigger": inv.trigger,
                                "outcome": inv.outcome
                            },
                            "event_b": {
                                "type": "drift",
                                "id": drift.id,
                                "time": drift.detected_at.isoformat(),
                                "drift_type": drift.drift_type,
                                "description": drift.description
                            },
                            "time_delta_seconds": delta,
                            "likely_related": delta < 60  # Very likely if < 1 minute
                        })

            # Find investigation <-> investigation correlations (cascade detection)
            for i, inv1 in enumerate(investigations):
                for inv2 in investigations[i+1:]:
                    delta = abs((inv1.started_at - inv2.started_at).total_seconds())
                    if delta <= window_seconds and delta > 0:
                        correlations.append({
                            "event_a": {
                                "type": "investigation",
                                "id": inv1.id,
                                "time": inv1.started_at.isoformat(),
                                "trigger": inv1.trigger,
                                "outcome": inv1.outcome
                            },
                            "event_b": {
                                "type": "investigation",
                                "id": inv2.id,
                                "time": inv2.started_at.isoformat(),
                                "trigger": inv2.trigger,
                                "outcome": inv2.outcome
                            },
                            "time_delta_seconds": delta,
                            "likely_related": delta < 60
                        })

            # Sort by time delta (closest events first)
            correlations.sort(key=lambda x: x["time_delta_seconds"])
            return correlations

    def find_service_failure_patterns(self, days: int = 30) -> List[Dict[str, Any]]:
        """Find patterns of which services fail together or in sequence."""
        from datetime import timedelta
        from collections import defaultdict

        with self.session_scope() as session:
            cutoff = datetime.now(timezone.utc) - timedelta(days=days)

            # Get service-related drift events
            drift_events = session.query(DriftEvent).filter(
                DriftEvent.detected_at >= cutoff,
                DriftEvent.drift_type.in_(['container_stopped', 'state_change', 'service_unhealthy', 'container_change'])
            ).order_by(DriftEvent.detected_at).all()

            # Extract service names from drift events
            service_events = []
            for drift in drift_events:
                details = drift.drift_details or {}
                service = details.get('service') or details.get('container') or details.get('service_name')
                if service:
                    service_events.append({
                        'service': service,
                        'time': drift.detected_at,
                        'type': drift.drift_type
                    })

            # Find co-occurring failures (within 5 minutes)
            co_failures = defaultdict(lambda: {"count": 0, "time_deltas": []})
            window = 300  # 5 minutes

            for i, event1 in enumerate(service_events):
                for event2 in service_events[i+1:]:
                    delta = (event2['time'] - event1['time']).total_seconds()
                    if delta > window:
                        break
                    if event1['service'] != event2['service']:
                        key = tuple(sorted([event1['service'], event2['service']]))
                        co_failures[key]["count"] += 1
                        co_failures[key]["time_deltas"].append(delta)

            # Convert to result format
            patterns = []
            for (service_a, service_b), data in co_failures.items():
                if data["count"] >= 2:  # Only report patterns with 2+ occurrences
                    avg_delta = sum(data["time_deltas"]) / len(data["time_deltas"])
                    patterns.append({
                        "service_a": service_a,
                        "service_b": service_b,
                        "co_failure_count": data["count"],
                        "avg_time_delta_seconds": round(avg_delta, 1),
                        "correlation_type": "cascade_failure" if avg_delta > 30 else "co_failure"
                    })

            # Sort by occurrence count
            patterns.sort(key=lambda x: x["co_failure_count"], reverse=True)
            return patterns

    def record_service_correlation(
        self,
        service_a: str,
        service_b: str,
        correlation_type: str,
        time_delta_seconds: Optional[float] = None,
        details: Optional[Dict[str, Any]] = None
    ) -> int:
        """Record or update a service correlation."""
        with self.session_scope() as session:
            # Check if correlation already exists
            existing = session.query(ServiceCorrelation).filter(
                ServiceCorrelation.service_a == service_a,
                ServiceCorrelation.service_b == service_b,
                ServiceCorrelation.correlation_type == correlation_type
            ).first()

            if existing:
                existing.occurrence_count += 1
                existing.last_seen = datetime.now(timezone.utc)
                if time_delta_seconds is not None:
                    # Update rolling average
                    old_avg = existing.avg_time_delta_seconds or time_delta_seconds
                    new_avg = (old_avg * (existing.occurrence_count - 1) + time_delta_seconds) / existing.occurrence_count
                    existing.avg_time_delta_seconds = new_avg
                if details:
                    existing.correlation_details = {**(existing.correlation_details or {}), **details}
                return existing.id
            else:
                correlation = ServiceCorrelation(
                    service_a=service_a,
                    service_b=service_b,
                    correlation_type=correlation_type,
                    avg_time_delta_seconds=time_delta_seconds,
                    correlation_details=details or {}
                )
                session.add(correlation)
                session.flush()
                return correlation.id

    def record_event_correlation(
        self,
        event_a_type: str,
        event_a_id: int,
        event_b_type: str,
        event_b_id: int,
        time_delta_seconds: float,
        correlation_window: int = 300,
        correlation_strength: Optional[float] = None,
        root_cause_candidate: Optional[str] = None,
        analysis_notes: Optional[str] = None
    ) -> Optional[int]:
        """Persist an event correlation to the database."""
        with self.session_scope() as session:
            # Avoid duplicate: same event pair already recorded
            existing = session.query(EventCorrelation).filter(
                EventCorrelation.event_a_type == event_a_type,
                EventCorrelation.event_a_id == event_a_id,
                EventCorrelation.event_b_type == event_b_type,
                EventCorrelation.event_b_id == event_b_id
            ).first()
            if existing:
                return existing.id

            # Compute strength from time delta if not provided
            if correlation_strength is None:
                # Closer in time = stronger correlation (linear decay within window)
                correlation_strength = max(0.0, 1.0 - (abs(time_delta_seconds) / correlation_window))

            ec = EventCorrelation(
                correlation_window_seconds=correlation_window,
                event_a_type=event_a_type,
                event_a_id=event_a_id,
                event_b_type=event_b_type,
                event_b_id=event_b_id,
                time_delta_seconds=time_delta_seconds,
                correlation_strength=round(correlation_strength, 3),
                root_cause_candidate=root_cause_candidate or 'unknown',
                analysis_notes=analysis_notes
            )
            session.add(ec)
            session.flush()
            return ec.id

    def get_service_correlations(self, min_count: int = 2) -> List[Dict[str, Any]]:
        """Get all learned service correlations above a threshold."""
        with self.session_scope() as session:
            correlations = session.query(ServiceCorrelation).filter(
                ServiceCorrelation.occurrence_count >= min_count
            ).order_by(ServiceCorrelation.occurrence_count.desc()).all()

            return [{
                "id": c.id,
                "first_seen": c.first_seen.isoformat() if c.first_seen else None,
                "last_seen": c.last_seen.isoformat() if c.last_seen else None,
                "service_a": c.service_a,
                "service_b": c.service_b,
                "correlation_type": c.correlation_type,
                "occurrence_count": c.occurrence_count,
                "avg_time_delta_seconds": c.avg_time_delta_seconds,
                "correlation_details": c.correlation_details
            } for c in correlations]

    def analyze_investigation_metrics(self, investigation_id: int) -> Dict[str, Any]:
        """Analyze metrics before and after an investigation to find anomalies."""
        with self.session_scope() as session:
            # Get the investigation
            inv = session.query(Investigation).filter_by(id=investigation_id).first()
            if not inv:
                return {"error": "Investigation not found"}

            # Get metric snapshots around this investigation
            from datetime import timedelta
            window_before = inv.started_at - timedelta(minutes=30)
            window_after = (inv.completed_at or inv.started_at) + timedelta(minutes=30)

            snapshots = session.query(MetricSnapshot).filter(
                MetricSnapshot.captured_at >= window_before,
                MetricSnapshot.captured_at <= window_after
            ).order_by(MetricSnapshot.captured_at).all()

            if not snapshots:
                return {
                    "investigation_id": investigation_id,
                    "trigger": inv.trigger,
                    "outcome": inv.outcome,
                    "snapshots_found": 0,
                    "analysis": "No metric snapshots available for correlation"
                }

            # Analyze metric changes
            before_inv = [s for s in snapshots if s.captured_at < inv.started_at]
            after_inv = [s for s in snapshots if s.captured_at > (inv.completed_at or inv.started_at)]

            analysis = {
                "investigation_id": investigation_id,
                "trigger": inv.trigger,
                "outcome": inv.outcome,
                "started_at": inv.started_at.isoformat(),
                "completed_at": inv.completed_at.isoformat() if inv.completed_at else None,
                "snapshots_found": len(snapshots),
                "before_count": len(before_inv),
                "after_count": len(after_inv),
                "metric_changes": []
            }

            # Compare metrics if we have before and after snapshots
            if before_inv and after_inv:
                before_metrics = before_inv[-1].metrics  # Latest before
                after_metrics = after_inv[0].metrics  # First after

                for key in set(before_metrics.keys()) | set(after_metrics.keys()):
                    before_val = before_metrics.get(key)
                    after_val = after_metrics.get(key)

                    if isinstance(before_val, (int, float)) and isinstance(after_val, (int, float)):
                        change = after_val - before_val
                        pct_change = (change / before_val * 100) if before_val != 0 else 0

                        if abs(pct_change) > 10:  # Significant change threshold
                            analysis["metric_changes"].append({
                                "metric": key,
                                "before": before_val,
                                "after": after_val,
                                "change": change,
                                "pct_change": round(pct_change, 1)
                            })

            return analysis

    def get_correlation_summary(self, hours: int = 24) -> Dict[str, Any]:
        """Get a summary of correlations found in the given time window."""
        correlated_events = self.find_correlated_events(window_seconds=300, hours=hours)
        service_patterns = self.find_service_failure_patterns(days=7)
        service_correlations = self.get_service_correlations(min_count=2)

        return {
            "time_window_hours": hours,
            "correlated_events": {
                "total": len(correlated_events),
                "likely_related": len([c for c in correlated_events if c.get("likely_related")]),
                "events": correlated_events[:20]  # Top 20
            },
            "service_failure_patterns": {
                "total": len(service_patterns),
                "patterns": service_patterns[:10]  # Top 10
            },
            "learned_correlations": {
                "total": len(service_correlations),
                "correlations": service_correlations[:10]  # Top 10
            }
        }


# ============================= Resilient Knowledge Base =======================

class ResilientKnowledgeBase:
    """
    Wrapper around KnowledgeBase that provides offline resilience.

    Behavior:
    - When PostgreSQL is healthy: Direct writes, no buffering
    - When PostgreSQL is down: Buffer events locally to JSON Lines files
    - Background thread syncs buffered events when connection restored

    Usage:
        # Use exactly like KnowledgeBase
        kb = ResilientKnowledgeBase()
        kb.initialize_schema()
        inv_id = kb.start_investigation("trigger")  # Works even when DB is down
    """

    def __init__(self, db_url: Optional[str] = None, host_id: Optional[str] = None):
        """Initialize resilient knowledge base with offline buffering."""
        # Initialize underlying KnowledgeBase
        self._kb = KnowledgeBase(db_url=db_url, host_id=host_id)
        self.host_id = self._kb.host_id

        # Health monitoring
        check_interval = int(os.getenv("SENTINEL_HEALTH_CHECK_INTERVAL", "10"))
        failure_threshold = int(os.getenv("SENTINEL_HEALTH_CHECK_FAILURES", "2"))
        self._health_monitor = ConnectionHealthMonitor(
            self._kb.engine,
            check_interval=check_interval,
            failure_threshold=failure_threshold
        )

        # Local buffer (import here to avoid circular dependency)
        from local_buffer import LocalEventBuffer
        buffer_dir = os.getenv("SENTINEL_BUFFER_DIR", "/data/buffer")
        self._buffer = LocalEventBuffer(
            host_id=self.host_id,
            buffer_dir=buffer_dir,
            max_file_size_mb=int(os.getenv("SENTINEL_BUFFER_FILE_SIZE_MB", "10")),
            max_total_size_mb=int(os.getenv("SENTINEL_BUFFER_TOTAL_SIZE_MB", "100"))
        )

        # Sync thread
        self._sync_thread = None
        self._stop_sync = threading.Event()
        self._start_sync_thread()

        # Track local investigation IDs (negative to avoid conflicts with DB IDs)
        self._local_inv_id_counter = -1
        self._local_id_lock = threading.Lock()
        self._local_to_db_id_map: Dict[int, int] = {}  # Maps local IDs to DB IDs after sync

        _log("info", "Resilient knowledge base initialized",
             host_id=self.host_id,
             buffer_dir=buffer_dir)

    def _start_sync_thread(self):
        """Start background sync thread."""
        self._sync_thread = threading.Thread(
            target=self._sync_loop,
            daemon=True,
            name="buffer-sync"
        )
        self._sync_thread.start()

    def _sync_loop(self):
        """Background loop that syncs buffered events when connection restored."""
        sync_interval = int(os.getenv("SENTINEL_SYNC_INTERVAL_SECONDS", "30"))

        while not self._stop_sync.is_set():
            try:
                if self._health_monitor.is_healthy() and self._buffer.has_pending_events():
                    self._sync_buffered_events()
            except Exception as e:
                _log("error", "Sync loop error", error=str(e))

            self._stop_sync.wait(timeout=sync_interval)

    def _sync_buffered_events(self):
        """Replay buffered events to PostgreSQL."""
        events = self._buffer.get_pending_events()
        if not events:
            return

        _log("info", "Syncing buffered events", count=len(events))

        synced_up_to = 0
        for event in events:
            try:
                self._replay_event(event)
                synced_up_to = event.sequence
            except Exception as e:
                _log("error", "Failed to replay event",
                     event_type=event.event_type,
                     sequence=event.sequence,
                     error=str(e))
                # Mark connection unhealthy and stop syncing
                self._health_monitor.mark_unhealthy()
                break

        if synced_up_to > 0:
            self._buffer.mark_synced(synced_up_to)
            _log("info", "Synced buffered events", synced_up_to=synced_up_to)

    def _replay_event(self, event):
        """Replay a single buffered event to the database."""
        from local_buffer import BufferedEvent
        data = event.data

        if event.event_type == 'start_investigation':
            db_id = self._kb.start_investigation(data['trigger'])
            # Map local ID to DB ID for future reference
            if 'local_id' in data:
                self._local_to_db_id_map[data['local_id']] = db_id

        elif event.event_type == 'investigation_event':
            # Resolve investigation ID (may be local or already synced)
            inv_id = self._resolve_investigation_id(data['investigation_id'])
            if inv_id and inv_id > 0:  # Only replay if we have a valid DB ID
                self._kb.record_investigation_event(
                    investigation_id=inv_id,
                    event_type=data['event_type'],
                    tool_name=data.get('tool_name'),
                    tool_input=data.get('tool_input'),
                    tool_output=data.get('tool_output'),
                    duration_ms=data.get('duration_ms'),
                    success=data.get('success', True),
                    reasoning_text=data.get('reasoning_text'),
                    action_type=data.get('action_type'),
                    action_target=data.get('action_target'),
                    error_message=data.get('error_message')
                )

        elif event.event_type == 'update_investigation':
            inv_id = self._resolve_investigation_id(data['investigation_id'])
            if inv_id and inv_id > 0:
                self._kb.update_investigation(
                    investigation_id=inv_id,
                    findings=data.get('findings'),
                    outcome=data.get('outcome'),
                    duration_seconds=data.get('duration_seconds'),
                    tool_calls_count=data.get('tool_calls_count')
                )

        elif event.event_type == 'record_drift':
            self._kb.record_drift_event(
                drift_type=data['drift_type'],
                description=data['description'],
                drift_details=data['drift_details']
            )

        elif event.event_type == 'update_baseline':
            self._kb.update_baseline(
                service_name=data['service_name'],
                expected_state=data['expected_state'],
                baseline_metrics=data.get('baseline_metrics', {})
            )

    def _resolve_investigation_id(self, id_value: int) -> Optional[int]:
        """Resolve investigation ID (local negative IDs map to DB IDs)."""
        if id_value >= 0:
            return id_value
        # Negative ID = local ID, look up mapping
        return self._local_to_db_id_map.get(id_value)

    def _get_local_id(self) -> int:
        """Get a unique local investigation ID (negative to avoid conflicts)."""
        with self._local_id_lock:
            self._local_inv_id_counter -= 1
            return self._local_inv_id_counter

    # ====================== Public API - Resilient Methods ======================

    def is_online(self) -> bool:
        """Check if database is reachable."""
        return self._health_monitor.is_healthy()

    def get_buffer_status(self) -> Dict[str, Any]:
        """Get status of local buffer and connection health."""
        return {
            "online": self.is_online(),
            "pending_events": self._buffer.pending_count(),
            "has_pending": self._buffer.has_pending_events(),
            "health": self._health_monitor.get_status()
        }

    def start_investigation(self, trigger: str) -> int:
        """Start investigation - buffers if offline."""
        if self._health_monitor.is_healthy():
            try:
                return self._kb.start_investigation(trigger)
            except Exception as e:
                self._health_monitor.mark_unhealthy()
                _log("warn", "start_investigation failed, buffering", error=str(e))

        # Offline - buffer and return local ID
        local_id = self._get_local_id()
        self._buffer.buffer_event('start_investigation', {
            'trigger': trigger,
            'local_id': local_id
        })
        return local_id

    def record_investigation_event(
        self,
        investigation_id: int,
        event_type: str,
        tool_name: Optional[str] = None,
        tool_input: Optional[Dict[str, Any]] = None,
        tool_output: Optional[Any] = None,
        duration_ms: Optional[int] = None,
        success: bool = True,
        reasoning_text: Optional[str] = None,
        action_type: Optional[str] = None,
        action_target: Optional[str] = None,
        error_message: Optional[str] = None
    ) -> int:
        """Record investigation event - buffers if offline."""
        if self._health_monitor.is_healthy() and investigation_id > 0:
            try:
                return self._kb.record_investigation_event(
                    investigation_id=investigation_id,
                    event_type=event_type,
                    tool_name=tool_name,
                    tool_input=tool_input,
                    tool_output=tool_output,
                    duration_ms=duration_ms,
                    success=success,
                    reasoning_text=reasoning_text,
                    action_type=action_type,
                    action_target=action_target,
                    error_message=error_message
                )
            except Exception as e:
                self._health_monitor.mark_unhealthy()
                _log("warn", "record_investigation_event failed, buffering", error=str(e))

        # Offline or local investigation - buffer
        self._buffer.buffer_event('investigation_event', {
            'investigation_id': investigation_id,
            'event_type': event_type,
            'tool_name': tool_name,
            'tool_input': tool_input,
            'tool_output': tool_output,
            'duration_ms': duration_ms,
            'success': success,
            'reasoning_text': reasoning_text,
            'action_type': action_type,
            'action_target': action_target,
            'error_message': error_message
        })
        return -1  # Placeholder ID

    def update_investigation(
        self,
        investigation_id: int,
        completed_at: Optional[datetime] = None,
        findings: Optional[Dict[str, Any]] = None,
        outcome: Optional[str] = None,
        duration_seconds: Optional[float] = None,
        tool_calls_count: Optional[int] = None
    ) -> bool:
        """Update investigation - buffers if offline."""
        if self._health_monitor.is_healthy() and investigation_id > 0:
            try:
                return self._kb.update_investigation(
                    investigation_id=investigation_id,
                    completed_at=completed_at,
                    findings=findings,
                    outcome=outcome,
                    duration_seconds=duration_seconds,
                    tool_calls_count=tool_calls_count
                )
            except Exception as e:
                self._health_monitor.mark_unhealthy()
                _log("warn", "update_investigation failed, buffering", error=str(e))

        # Offline or local investigation - buffer
        self._buffer.buffer_event('update_investigation', {
            'investigation_id': investigation_id,
            'findings': findings,
            'outcome': outcome,
            'duration_seconds': duration_seconds,
            'tool_calls_count': tool_calls_count
        })
        return True

    def record_drift_event(self, drift_type: str, description: str, drift_details: Dict[str, Any]) -> int:
        """Record drift event - buffers if offline."""
        if self._health_monitor.is_healthy():
            try:
                return self._kb.record_drift_event(drift_type, description, drift_details)
            except Exception as e:
                self._health_monitor.mark_unhealthy()
                _log("warn", "record_drift_event failed, buffering", error=str(e))

        # Offline - buffer
        self._buffer.buffer_event('record_drift', {
            'drift_type': drift_type,
            'description': description,
            'drift_details': drift_details
        })
        return -1  # Placeholder ID

    def update_baseline(self, service_name: str, expected_state: str, baseline_metrics: Dict[str, Any]) -> int:
        """Update baseline - buffers if offline."""
        if self._health_monitor.is_healthy():
            try:
                return self._kb.update_baseline(service_name, expected_state, baseline_metrics)
            except Exception as e:
                self._health_monitor.mark_unhealthy()
                _log("warn", "update_baseline failed, buffering", error=str(e))

        # Offline - buffer
        self._buffer.buffer_event('update_baseline', {
            'service_name': service_name,
            'expected_state': expected_state,
            'baseline_metrics': baseline_metrics
        })
        return -1  # Placeholder ID

    # ====================== Read Operations - Graceful Degradation ==============

    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Get setting - returns default if offline."""
        if self._health_monitor.is_healthy():
            try:
                return self._kb.get_setting(key, default)
            except Exception:
                self._health_monitor.mark_unhealthy()
        return default

    def get_next_queued_investigation(self) -> Optional[Dict[str, Any]]:
        """Get next queued investigation - returns None if offline."""
        if self._health_monitor.is_healthy():
            try:
                return self._kb.get_next_queued_investigation()
            except Exception:
                self._health_monitor.mark_unhealthy()
        return None  # Can't process queue when offline

    def get_baseline(self, service_name: Optional[str] = None) -> Dict[str, Any]:
        """Get baseline - returns empty dict if offline."""
        if self._health_monitor.is_healthy():
            try:
                return self._kb.get_baseline(service_name)
            except Exception:
                self._health_monitor.mark_unhealthy()
        return {}

    def get_stats(self) -> Dict[str, Any]:
        """Get stats - includes buffer status."""
        stats = {"enabled": True, "db_type": "postgresql", "online": self.is_online()}
        if self._health_monitor.is_healthy():
            try:
                db_stats = self._kb.get_stats()
                stats.update(db_stats)
            except Exception:
                self._health_monitor.mark_unhealthy()
        stats["buffer_pending"] = self._buffer.pending_count()
        return stats

    def find_learnings(self, **kwargs):
        """Find learnings - returns empty list if offline."""
        if self._health_monitor.is_healthy():
            try:
                return self._kb.find_learnings(**kwargs)
            except Exception:
                self._health_monitor.mark_unhealthy()
        return []

    def store_learning(self, learning_data):
        """Store learning - silently fails if offline."""
        if self._health_monitor.is_healthy():
            try:
                return self._kb.store_learning(learning_data)
            except Exception as e:
                self._health_monitor.mark_unhealthy()
                _log("warn", "store_learning failed (DB down?)", error=str(e))
        return -1

    def get_learnings_since(self, since, limit=50):
        """Get learnings since timestamp - returns empty list if offline."""
        if self._health_monitor.is_healthy():
            try:
                return self._kb.get_learnings_since(since, limit)
            except Exception:
                self._health_monitor.mark_unhealthy()
        return []

    # ====================== Delegate Everything Else to Underlying KB ===========

    def __getattr__(self, name):
        """Delegate any other method calls to the underlying KnowledgeBase."""
        return getattr(self._kb, name)

    def close(self):
        """Cleanup resources."""
        self._stop_sync.set()
        if self._sync_thread:
            self._sync_thread.join(timeout=5)
        self._buffer.close()
