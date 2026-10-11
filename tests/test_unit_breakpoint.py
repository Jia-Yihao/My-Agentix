"""Unit tests for breakpoint resume module."""

import time
from agentix.application.services.breakpoint_resume import (
    InterruptRecord,
    InterruptReason,
    ResumeAction,
)


class TestInterruptReason:
    """Tests for InterruptReason enum."""

    def test_all_reasons(self):
        assert InterruptReason.HUMAN_APPROVAL.value == "human_approval"
        assert InterruptReason.SERVICE_FAILURE.value == "service_failure"
        assert InterruptReason.RATE_LIMIT.value == "rate_limit"
        assert InterruptReason.MANUAL_PAUSE.value == "manual_pause"
        assert InterruptReason.TIMEOUT.value == "timeout"


class TestResumeAction:
    """Tests for ResumeAction enum."""

    def test_all_actions(self):
        assert ResumeAction.CONTINUE.value == "continue"
        assert ResumeAction.RETRY.value == "retry"
        assert ResumeAction.SKIP.value == "skip"
        assert ResumeAction.ABORT.value == "abort"
        assert ResumeAction.OVERRIDE.value == "override"


class TestInterruptRecord:
    """Tests for InterruptRecord dataclass."""

    def test_creation(self):
        record = InterruptRecord(
            interrupt_id="int1",
            thread_id="thread1",
            node_name="create_research_plan",
            reason=InterruptReason.SERVICE_FAILURE,
            error="Neo4j connection refused",
        )
        assert record.interrupt_id == "int1"
        assert record.thread_id == "thread1"
        assert record.reason == InterruptReason.SERVICE_FAILURE
        assert record.retry_count == 0
        assert record.max_retries == 3

    def test_to_dict(self):
        record = InterruptRecord(
            interrupt_id="i1",
            thread_id="t1",
            node_name="node1",
            reason=InterruptReason.HUMAN_APPROVAL,
        )
        d = record.to_dict()
        assert d["interrupt_id"] == "i1"
        assert d["reason"] == "human_approval"
        assert d["resolved_at"] is None
        assert d["resolution"] is None

    def test_from_dict(self):
        data = {
            "interrupt_id": "i2",
            "thread_id": "t2",
            "node_name": "node2",
            "reason": "service_failure",
            "error": "timeout",
            "retry_count": 2,
            "max_retries": 5,
        }
        record = InterruptRecord.from_dict(data)
        assert record.interrupt_id == "i2"
        assert record.reason == InterruptReason.SERVICE_FAILURE
        assert record.retry_count == 2

    def test_roundtrip(self):
        original = InterruptRecord(
            interrupt_id="rt",
            thread_id="t",
            node_name="n",
            reason=InterruptReason.RATE_LIMIT,
            error="429 Too Many Requests",
            retry_count=1,
        )
        restored = InterruptRecord.from_dict(original.to_dict())
        assert restored.interrupt_id == original.interrupt_id
        assert restored.reason == original.reason
        assert restored.retry_count == original.retry_count

    def test_with_resolution(self):
        record = InterruptRecord(
            interrupt_id="resolved",
            thread_id="t",
            node_name="n",
            reason=InterruptReason.SERVICE_FAILURE,
            resolved_at=time.time(),
            resolution=ResumeAction.RETRY,
        )
        d = record.to_dict()
        assert d["resolution"] == "retry"
        assert d["resolved_at"] is not None

        restored = InterruptRecord.from_dict(d)
        assert restored.resolution == ResumeAction.RETRY
