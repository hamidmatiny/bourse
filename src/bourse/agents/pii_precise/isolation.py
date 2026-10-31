"""CrewAI process isolation (ADR-0021). Apply before the first `import crewai`.

crewai 1.6.1 pings api.scarf.sh on import and starts OpenTelemetry/tracing unless these are
set; CREWAI_TESTING=true suppresses its interactive first-run tracing prompt on stdin.
OTEL_SDK_DISABLED disables OpenTelemetry for this agent process only.
"""

import os

CREWAI_ENV = {
    "CREWAI_DISABLE_TELEMETRY": "true",
    "CREWAI_DISABLE_TRACKING": "true",
    "CREWAI_TRACING_ENABLED": "false",
    "CREWAI_TESTING": "true",
    "CREWAI_STORAGE_DIR": "bourse-crewai",
    "OTEL_SDK_DISABLED": "true",
    "ANONYMIZED_TELEMETRY": "False",
}


def apply() -> None:
    os.environ.update(CREWAI_ENV)
