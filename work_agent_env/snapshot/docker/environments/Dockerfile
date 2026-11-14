ARG BASE_IMAGE=toolathlon-pack:api-env-audit7-20261026
FROM ${BASE_IMAGE}
# Existing runtime contains all 25 MCP server packages, browser and DB clients.
# The base digest is recorded by deployment/probe receipts, not assumed from tag.
COPY environments /opt/workenv_environment
ENV PYTHONPATH=/opt:/workspace
RUN /opt/venv/bin/python3 -c "import yaml,jsonschema; from workenv_environment import ToolathlonEnvironment; print('Environment module import OK')"
