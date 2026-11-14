"""Owned PostgreSQL slots for original-task isolation, retaining all diagnostics.

The pinned evaluator fixes port/database but reads PGHOST. Each slot therefore
uses its own loopback address and data directory, with the original database name.
These slots do not enable concurrent NeMo rollouts by themselves.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import uuid

from workenv.core import ResourceError, contained, no_links, read_json, save
from workenv.leases import owned_lock
from workenv.process_identity import process_identity
from workenv.postgres_binary import postgres_binary

HOSTS = ("127.77.0.21", "127.77.0.22")


@dataclass(frozen=True)
class EpisodeProfile:
    owner: Path
    resource_id: str
    slot: int

    def __post_init__(self):
        object.__setattr__(self, "owner", Path(self.owner).resolve())
        if not isinstance(self.resource_id, str) or str(uuid.UUID(self.resource_id)) != self.resource_id:
            raise ValueError("Episode resource identity must be a canonical UUID")
        if type(self.slot) is not int or self.slot not in range(len(HOSTS)):
            raise ValueError("Only the two reserved loopback resource slots are supported")
        no_links(self.base, self.owner)

    @property
    def base(self):
        return contained(self.owner, ".runtime/native-episodes/" + self.resource_id)

    @property
    def host(self):
        return HOSTS[self.slot]

    @property
    def data(self):
        return contained(self.base, "pgdata")

    def record(self):
        return {"schema_version": "native-episode-profile-v1", "workspace_owner": str(self.owner),
                "resource_id": self.resource_id, "slot": self.slot, "host": self.host, "port": 5432,
                "database": "toolathlon_gym", "data_directory": self.data.relative_to(self.owner).as_posix()}

    def validate_record(self, record):
        if record != self.record():
            raise ValueError("Episode profile was rebound or changed")

    def environment(self, baseline=None):
        result = dict(os.environ if baseline is None else baseline)
        result.update({"PGHOST": self.host, "PG_HOST": self.host, "PGPORT": "5432", "PG_PORT": "5432",
                       "PGDATABASE": "toolathlon_gym", "PG_DATABASE": "toolathlon_gym", "PGUSER": "eigent",
                       "PG_USER": "eigent", "PGPASSWORD": "camel", "PG_PASSWORD": "camel",
                       "PGCONNECT_TIMEOUT": "3", "PGCLIENTENCODING": "UTF8", "PYTHONIOENCODING": "utf-8"})
        result["WORKENV_WOOCOMMERCE_MODULE_ID"] = self.resource_id
        return result


def require_dependents_closed(profile):
    """PG shutdown alone does not release an address used by an uncertain child."""
    for name in ("calendar-process-unknown.json", "recipe-process-unknown.json", "woocommerce-process-unknown.json"):
        path = profile.base / name
        no_links(path, profile.owner)
        if path.exists(): raise ResourceError("Episode dependent process shutdown is unknown; retain the slot")
    path = profile.base / "dependent_processes.json"
    no_links(path, profile.owner)
    if not path.exists(): return
    if path.stat().st_size > 32 * 1024: raise ResourceError("Episode dependent process record exceeds budget")
    try:
        record = read_json(path)
        expected, children = record["expected_children"], record["children"]
        if (record.get("schema_version") != "native-episode-dependents-v1" or record.get("profile") != profile.record()
            or record.get("state") != "closed" or not isinstance(expected, list) or not 1 <= len(expected) <= 16
            or any(not isinstance(n, str) or not 1 <= len(n) <= 100 for n in expected)
            or len(set(expected)) != len(expected) or not isinstance(children, dict) or set(children) != set(expected)):
            raise ValueError("Incomplete dependent process record")
        for child in children.values():
            before, after = child["before"], child["after"]
            if not isinstance(before, dict) or not isinstance(after, dict): raise ValueError("Malformed dependent child identity")
            same_pid = type(after.get("pid")) is int and after["pid"] == before["pid"]
            reused_pid = (after.get("status") == "alive" and isinstance(after.get("creation_id"), str)
                          and bool(after["creation_id"]) and after["creation_id"] != before["creation_id"])
            closed = same_pid and (after.get("status") in ("not_found", "exited") or reused_pid)
            if (child.get("profile") != profile.record() or child.get("closed") is not True
                or before["status"] != "alive" or type(before.get("pid")) is not int or before["pid"] <= 0
                or not isinstance(before.get("creation_id"), str) or not before["creation_id"] or not closed):
                raise ValueError("Dependent child closure was not confirmed")
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise ResourceError("Episode dependent process closure is unknown; retain the slot") from exc


def probe_endpoint(host, port):
    """Reject live listeners while allowing Linux TIME_WAIT after a clean stop."""
    with socket.socket() as probe:
        if sys.platform == "linux":
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind((host, port))


class EpisodeCluster:
    def __init__(self, profile):
        self.profile = profile
        self.started = False
        self.identity = None

    def require_owner(self):
        no_links(self.profile.base / "cluster_owner.json", self.profile.owner)
        self.profile.validate_record(read_json(self.profile.base / "cluster_owner.json"))
        no_links(self.profile.data, self.profile.owner)

    def command(self, program, args, *, timeout=90):
        self.require_owner()
        binary = postgres_binary(self.profile.owner, program)
        stem = self.profile.base / (program + "_" + uuid.uuid4().hex)
        with stem.with_suffix(".stdout.txt").open("xb") as out, stem.with_suffix(".stderr.txt").open("xb") as err:
            result = subprocess.run([str(binary), *map(str, args)], env=self.profile.environment(),
                stdin=subprocess.DEVNULL, stdout=out, stderr=err, timeout=timeout,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        save(stem.with_suffix(".json"), {"program": program, "exit_code": result.returncode,
                                       "resource_id": self.profile.resource_id})
        if result.returncode:
            raise RuntimeError("Isolated PostgreSQL command failed; inspect " + stem.name)

    def connect(self, database="toolathlon_gym"):
        self.require_owner()
        if database not in ("toolathlon_gym", "postgres"):
            raise ValueError("Unexpected isolated database name")
        import psycopg2
        conn = psycopg2.connect(host=self.profile.host, port=5432, dbname=database,
                                user="eigent", password="camel", connect_timeout=3)
        try:
            with conn.cursor() as cur:
                cur.execute("SHOW data_directory")
                if Path(cur.fetchone()[0]).resolve() != self.profile.data.resolve():
                    raise RuntimeError("Connected cluster is outside this episode resource")
            return conn
        except Exception:
            conn.close()
            raise

    def start(self, seed_sql):
        self.require_owner()
        if self.started or self.profile.data.exists():
            raise ValueError("An isolated cluster may only be initialized once")
        seed_sql = Path(seed_sql).absolute()
        no_links(seed_sql, self.profile.owner)
        if not seed_sql.is_relative_to(self.profile.owner / ".runtime/native-episodes") or not seed_sql.is_file() or seed_sql.stat().st_size > 16 * 1024 * 1024:
            raise ValueError("Expected a bounded owned schema seed file")
        probe_endpoint(self.profile.host, 5432)  # Refuse occupied slots, never stop their owners.
        password_file = self.profile.base / "mock-password.txt"
        password_file.write_text("camel\n", encoding="ascii")
        try:
            self.command("initdb", ["-D", self.profile.data, "-U", "eigent", "--encoding=UTF8", "--locale=C",
                                   "--auth-host=md5", "--auth-local=trust", "--pwfile=" + str(password_file)])
        finally:
            password_file.unlink(missing_ok=True)
        self.started = True  # A partial start still requires cleanup or a retained unknown slot.
        options = f"-h {self.profile.host} -p 5432 -c shared_buffers=16MB -c max_connections=8"
        if os.name != "nt":
            options += " -c unix_socket_directories="
        self.command("pg_ctl", ["-D", self.profile.data, "-l", self.profile.base / "postgres.log", "-o",
            options, "-w", "start"], timeout=45)
        pid = int((self.profile.data / "postmaster.pid").read_text().splitlines()[0])
        self.identity = process_identity(pid)
        if self.identity["status"] != "alive" or not self.identity["creation_id"]:
            raise RuntimeError("Cannot attest the isolated postmaster creation identity")
        save(self.profile.base / "postmaster_identity.json", self.identity)
        conn = self.connect("postgres")
        conn.close()
        self.command("createdb", ["-h", self.profile.host, "-p", "5432", "-U", "eigent", "toolathlon_gym"])
        self.command("psql", ["-h", self.profile.host, "-p", "5432", "-U", "eigent", "-d", "toolathlon_gym",
                              "--set=ON_ERROR_STOP=1", "--file=" + str(seed_sql)])
        save(self.profile.base / "seed_identity.json", {"seed_sha256": hashlib.sha256(seed_sql.read_bytes()).hexdigest()})

    def stop(self):
        self.require_owner()
        if not self.started: return
        pidfile = self.profile.data / "postmaster.pid"
        no_links(pidfile, self.profile.owner)
        if not pidfile.exists():
            # No startup PID was created, or an already attested postmaster exited.
            if self.identity and process_identity(self.identity["pid"])["status"] not in ("not_found", "exited"):
                raise RuntimeError("Isolated postmaster ownership is uncertain; retain the slot")
            self.started = False
            return
        pid = int(pidfile.read_text().splitlines()[0])
        actual = process_identity(pid)
        if self.identity and actual != self.identity:
            raise RuntimeError("Isolated postmaster PID identity changed; no stop attempted")
        if actual["status"] in ("not_found", "exited"):
            raise RuntimeError("Stale isolated postmaster PID file; retain for explicit recovery")
        conn = self.connect("postgres")
        conn.close()
        self.command("pg_ctl", ["-D", self.profile.data, "-m", "fast", "-w", "stop"], timeout=45)
        if pidfile.exists(): raise RuntimeError("Isolated postmaster shutdown was not confirmed")
        self.started = False


@contextmanager
def reserve_cluster(root, *, slot, resource_id=None):
    profile = EpisodeProfile(root, resource_id or str(uuid.uuid4()), slot)
    directory = contained(profile.owner, ".runtime/native-episodes")
    directory.mkdir(parents=True, exist_ok=True)
    slotfile = directory / ("slot-" + str(slot) + ".json")
    with owned_lock(directory / ("slot-" + str(slot) + ".lock"), profile.owner, "native_episode_slot"):
        no_links(slotfile, profile.owner)
        if slotfile.exists():
            old = read_json(slotfile)
            if old.get("workspace_owner") != str(profile.owner) or old.get("state") != "stopped" or old.get("slot") != slot:
                raise RuntimeError("Previous slot has not been confirmed stopped; preserve it for recovery")
            require_dependents_closed(EpisodeProfile(profile.owner, old["resource_id"], slot))
        profile.base.mkdir(exist_ok=False)
        save(profile.base / "cluster_owner.json", profile.record())
        record = {"workspace_owner": str(profile.owner), "slot": slot, "resource_id": profile.resource_id, "state": "reserved"}
        save(slotfile, record)
        cluster = EpisodeCluster(profile)
        try:
            yield cluster
        finally:
            try:
                cluster.stop()
                require_dependents_closed(profile)
                save(slotfile, {**record, "state": "stopped"})
            except Exception:
                save(slotfile, {**record, "state": "unknown"})
                raise


def recover_orphan_cluster(root, *, slot, resource_id):
    """Recover an abandoned slot only after checking lease and postmaster owners.

    A live or unqueryable lease owner is never stopped. Retain stale PID files and
    uncertain clusters for explicit diagnosis rather than recycling their slots.
    """
    profile = EpisodeProfile(root, resource_id, slot)
    cluster = EpisodeCluster(profile)
    cluster.require_owner()
    directory = profile.base.parent
    slotfile = directory / ("slot-" + str(slot) + ".json")
    lockpath = directory / ("slot-" + str(slot) + ".lock")
    no_links(slotfile, profile.owner)
    record = read_json(slotfile)
    if record.get("workspace_owner") != str(profile.owner) or record.get("resource_id") != resource_id or record.get("slot") != slot:
        raise ValueError("Slot belongs to another resource")
    require_dependents_closed(profile)  # Never archive a lease or release a slot before dependent closure.
    no_links(lockpath, profile.owner)
    archive = None
    if lockpath.exists():
        if lockpath.stat().st_size > 8192: raise ValueError("Unexpected isolated resource lease size")
        raw = lockpath.read_bytes()
        lease = json.loads(raw)
        if (lease.get("schema_version") != "workenv-lock-v0.1" or lease.get("workspace_owner") != str(profile.owner)
            or lease.get("resource") != lockpath.relative_to(profile.owner).as_posix() or lease.get("kind") != "native_episode_slot"):
            raise ValueError("Lease ownership does not match this isolated resource")
        owner = process_identity(lease["process"]["pid"])
        if owner["status"] not in ("not_found", "exited"):
            raise RuntimeError("Episode lease owner is alive or unknown; no recovery attempted")
        archive = profile.base / ("recovered-slot-lease-" + uuid.uuid4().hex + ".json")
        with archive.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if lockpath.read_bytes() != raw: raise RuntimeError("Episode lease changed during recovery")
        lockpath.unlink()
    with owned_lock(lockpath, profile.owner, "native_episode_recovery"):
        if read_json(slotfile) != record:
            raise RuntimeError("Isolated slot identity changed during recovery")
        try:
            pidfile = profile.data / "postmaster.pid"
            if pidfile.exists():
                identity_path = profile.base / "postmaster_identity.json"
                no_links(identity_path, profile.owner)
                if identity_path.stat().st_size > 8192: raise ValueError("Unexpected postmaster identity record")
                cluster.identity = read_json(identity_path)
                cluster.started = True
                cluster.stop()
            save(slotfile, {**record, "state": "stopped"})
        except Exception:
            save(slotfile, {**record, "state": "unknown"})
            raise
    return {"resource_id": resource_id, "slot": slot, "state": "stopped", "data_retained": True,
            "lease_archive_ref": archive.relative_to(profile.owner).as_posix() if archive else None}
