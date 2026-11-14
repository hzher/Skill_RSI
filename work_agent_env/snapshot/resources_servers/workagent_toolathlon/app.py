"""NeMo component entrypoint for the original Toolathlon task resources."""
from integrations.nemo_resources import ToolathlonResources

if __name__ == "__main__":
    ToolathlonResources.run_webserver()
