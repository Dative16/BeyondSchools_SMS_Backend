"""Logging, configured once at startup.

Until this existed there was no configuration at all, so the root logger sat at
its default WARNING and every `logger.info` in the codebase went nowhere. The
SQL echo appeared anyway, because SQLAlchemy configures its own logger, which
made it look as though logging worked.

The thing that made it worth fixing is the mock SMS sender. Its docstring says
a developer running the admissions flow needs to read the one-time code out of
the log, and that was simply not true: the line was written and dropped, and
there is no other way to get the code, because it is hashed before it is
stored. The applicant flow could not be exercised locally at all.
"""

from __future__ import annotations

import logging
from logging.config import dictConfig

from app.core.config import settings


def configure_logging() -> None:
    """One format, one level, and nothing that invents its own handler.

    Uvicorn's access log is left where it is: it already carries the request
    line and the status, and routing it through here only duplicates it.
    """
    level = (settings.LOG_LEVEL or "INFO").upper()

    dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "plain": {
                    "format": "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                    "datefmt": "%H:%M:%S",
                },
            },
            "handlers": {
                "stderr": {
                    "class": "logging.StreamHandler",
                    "formatter": "plain",
                },
            },
            "root": {"handlers": ["stderr"], "level": level},
            "loggers": {
                # Noisy at INFO and rarely what anybody is looking for.
                "httpx": {"level": "WARNING"},
                "httpcore": {"level": "WARNING"},
                "botocore": {"level": "WARNING"},
                # SQL echo follows DEBUG, which is what the engine is built
                # with. Turning it on from here would be a second switch.
                "sqlalchemy.engine": {"level": "INFO" if settings.DEBUG else "WARNING"},
            },
        }
    )

    if level == "DEBUG":
        logging.getLogger("app").debug("logging configured at DEBUG")
