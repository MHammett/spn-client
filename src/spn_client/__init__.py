"""spn-client: a hardened client for archive.org's availability and Save Page Now APIs."""

from spn_client.client import (
    ARCHIVE_ARCHIVED,
    ARCHIVE_CAPTURE_FAILED,
    ARCHIVE_NOT_ATTEMPTED,
    ARCHIVE_OUTCOME_LABELS,
    ARCHIVE_PENDING,
    ARCHIVE_SUBMIT_FAILED,
    ARCHIVE_SUBMITTED,
    DEFAULT_STALE_DAYS,
    capture_capacity,
    categorize_job_error,
    check,
    check_job_status,
    rate_limited_out,
    reset_rate_limit_state,
    service_health_note,
    snapshot_raw_url,
    submit,
    system_status,
)

__version__ = "0.3.0"

__all__ = [
    "ARCHIVE_ARCHIVED",
    "ARCHIVE_CAPTURE_FAILED",
    "ARCHIVE_NOT_ATTEMPTED",
    "ARCHIVE_OUTCOME_LABELS",
    "ARCHIVE_PENDING",
    "ARCHIVE_SUBMIT_FAILED",
    "ARCHIVE_SUBMITTED",
    "DEFAULT_STALE_DAYS",
    "capture_capacity",
    "categorize_job_error",
    "check",
    "check_job_status",
    "rate_limited_out",
    "reset_rate_limit_state",
    "service_health_note",
    "snapshot_raw_url",
    "submit",
    "system_status",
]
