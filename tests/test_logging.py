import logging

from sparser.logging_config import configure_progress_logging


def test_progress_logger_is_visible_and_idempotent():
    configure_progress_logging()
    configure_progress_logging()
    logger = logging.getLogger("sparser.pipeline")
    assert logger.isEnabledFor(logging.INFO)
    package_handlers = [
        h for h in logging.getLogger("sparser").handlers
        if getattr(h, "_sparser_progress", False)
    ]
    assert len(package_handlers) == 1
