"""Restricted entry to original PG email services; no real mailbox configuration."""
import argparse
import json
import os
from pathlib import Path
import socket
import sys

ROOT = Path(__file__).resolve().parents[1]
METHODS = ("send_email", "get_emails", "read_email")
SENDER = "vip-program@store.example.com"


class ToolRegistrar:
    """Register only the enumerated original decorated functions."""
    def __init__(self, server): self.server = server

    def tool(self, *args, **kwargs):
        def register(function):
            return self.server.tool(*args, **kwargs)(function) if function.__name__ in METHODS else function
        return register


def require_pg_environment(profile, environment):
    expected = profile.environment({})
    keys = ("PGHOST", "PG_HOST", "PGPORT", "PG_PORT", "PGDATABASE", "PG_DATABASE", "PGUSER", "PG_USER", "PGPASSWORD", "PG_PASSWORD")
    if any(environment.get(k) != expected[k] for k in keys): raise ValueError("Email PG environment was rebound")
    if any(environment.get(k) for k in ("PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE", "PGOPTIONS")):
        raise ValueError("Email PG indirect connection settings are not supported")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resource-id", required=True)
    parser.add_argument("--slot", type=int, choices=(0, 1), required=True)
    parser.add_argument("--task-id", default="wc-customer-order-gsheet-email")
    args = parser.parse_args()
    sys.path.insert(0, str(ROOT))
    from integrations.email_build import verify_build
    from integrations.episode_cluster import EpisodeProfile, EpisodeCluster
    from integrations import email_task_identity as task_identity
    from workenv.process_identity import process_identity
    from workenv.core import save

    profile = EpisodeProfile(ROOT, args.resource_id, args.slot)
    cluster = EpisodeCluster(profile); cluster.require_owner()
    # connect() independently checks the actual libpq data_directory before the
    # local attachment flag can satisfy the task binding's active-cluster gate.
    connection = cluster.connect(); connection.close(); cluster.started = True
    identity = task_identity.EmailTaskIdentity(args.task_id)
    directory = profile.base / "email-components-run"
    launch, _ = task_identity._read(ROOT, directory / "launch.json", limit=2 * 1024 * 1024)
    binding = task_identity.validate_launch(ROOT, cluster, launch, task_id=identity.task_id)
    base, _ = verify_build(ROOT, launch["build"], check_environment=True)
    require_pg_environment(profile, os.environ)

    def audit_socket(event, arguments):
        if event == "socket.connect":
            address = arguments[1]
            if not isinstance(address, tuple) or address[:2] != (profile.host, 5432):
                raise PermissionError("Owned email component permits only its PG socket endpoint")
    sys.addaudithook(audit_socket)
    with socket.socket() as probe:
        try: probe.connect(("127.0.0.1", 25))
        except PermissionError: forbidden_socket_blocked = True
        else: raise RuntimeError("Email transport guard did not reject SMTP endpoint")

    # Keep the package's original service and tool functions unchanged. Its
    # services already instantiate PgIMAPBackend/PgSMTPBackend, not real mail.
    sys.path.insert(0, str(base / "src"))
    from mcp.server.fastmcp import FastMCP
    from emails_mcp.models.config import EmailConfig
    from emails_mcp.config import config_manager
    from emails_mcp.services.email_service import EmailService
    from emails_mcp.backends.pg_backend import PgIMAPBackend, PgSMTPBackend
    from emails_mcp.tools.email_tools import register_email_tools

    files = directory / "files"; files.mkdir()
    config_manager.load_workspace_config(attachment_upload_path=str(files), attachment_download_path=str(files), email_export_path=str(files))
    service = EmailService(EmailConfig(email=identity.sender, password="unused", name=""))
    try:
        if type(service.imap_backend) is not PgIMAPBackend or type(service.smtp_backend) is not PgSMTPBackend:
            raise RuntimeError("Original email service did not select PG backends")
        if service.check_connection() != (True, True): raise RuntimeError("Owned PG email connections did not initialize")
        for backend in (service.imap_backend, service.smtp_backend):
            with backend.connection.cursor() as cursor:
                cursor.execute("SHOW data_directory")
                if Path(cursor.fetchone()[0]).resolve() != profile.data.resolve():
                    raise RuntimeError("Original email backend connected outside its owned PG")
        server = FastMCP("workenv-original-email-pg")
        register_email_tools(ToolRegistrar(server), service)
        save(directory / "email-process.json", {"schema_version": "owned-email-task-process-v1",
            "task_binding": binding, "process": process_identity(os.getpid()), "pid": os.getpid(), "profile": profile.record(),
            "backend_classes": [type(b).__module__ + "." + type(b).__name__ for b in (service.imap_backend, service.smtp_backend)],
            "pg_data_ref": profile.data.relative_to(ROOT).as_posix(), "forbidden_socket_blocked": forbidden_socket_blocked,
            "transport_scope": "Original PG backends; Python socket guard plus independent libpq data-directory checks",
            "external_messages_sent": 0, "methods": list(METHODS), "build_fingerprint": launch["build"]["build_fingerprint"]})
        server.run(transport="stdio")
    finally:
        service.cleanup()


if __name__ == "__main__": main()
