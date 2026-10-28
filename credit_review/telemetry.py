"""Traces to Application Insights, when the service runs in Azure.

Set up only when APPLICATIONINSIGHTS_CONNECTION_STRING is present. Otherwise the OpenTelemetry API stays a
no-op, so the CLI and the tests export nothing.

The App Insights resource has local (key) authentication disabled, so ingestion signs in with Entra ID. In
Azure that is the app's managed identity (AZURE_CLIENT_ID picks the user-assigned one), which holds the
Monitoring Metrics Publisher role.

What is traced: one span per assessment (dossier id, document count, outcome counts) with one child span per
model call (model, tokens, cost, cached). The document text and model answers are never put on a span.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)
_configured = False


def setup(service_name: str = "credit-api") -> bool:
    """Configure the Azure Monitor exporter once; True if telemetry is now exported."""
    global _configured
    if _configured:
        return True
    if not os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING"):
        return False
    from azure.identity import DefaultAzureCredential
    from azure.monitor.opentelemetry import configure_azure_monitor

    os.environ.setdefault("OTEL_SERVICE_NAME", service_name)
    configure_azure_monitor(credential=DefaultAzureCredential())
    _configured = True
    log.info("telemetry: exporting to Application Insights")
    return True
