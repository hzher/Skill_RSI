"""Owned per-run SQLite service, explicit tools and transactional audit history."""
from contextlib import contextmanager,closing
import csv
from datetime import datetime
import hashlib
import io
import json
from copy import deepcopy
import sqlite3

from workenv import core
from workenv.core import contained,now,owned_run,ResourceError
from workenv.file_tools import FileTools
from workenv.tool_contract import SERVICE_ARGUMENTS as ARGUMENTS,public_schemas,validate_arguments

APPLICATION_ID=0x57414F46
TICKET_FIELDS=("ticket_id","requester","title","category","priority","status","team","assignee","due_at")


# Compatibility snapshot. Published observations always request a fresh copy.
SERVICE_SCHEMAS=public_schemas("local_tickets")[3:]


class TicketStore:
    def __init__(self,run):
        self.run=run
        self.path=contained(run,"resources/tickets.sqlite")

    def initialize(self,inputs):
        metadata=owned_run(self.run)
        self.path.parent.mkdir(parents=True,exist_ok=True)
        if self.path.exists():raise ValueError("Refusing to overwrite an existing service database")
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute(f"PRAGMA application_id={APPLICATION_ID}")
            conn.execute("CREATE TABLE owner(run_id TEXT,task_id TEXT,workspace TEXT)")
            conn.execute("INSERT INTO owner VALUES (?,?,?)",(self.run.name,metadata["task_id"],str(core.ROOT)))
            conn.execute("CREATE TABLE tickets(ticket_id TEXT PRIMARY KEY,requester TEXT,title TEXT,category TEXT,priority TEXT,status TEXT,team TEXT,assignee TEXT,due_at TEXT)")
            conn.execute("CREATE TABLE directory(person TEXT PRIMARY KEY,team TEXT,active INTEGER)")
            conn.execute("CREATE TABLE audit(seq INTEGER PRIMARY KEY AUTOINCREMENT,ticket_id TEXT,operation TEXT,before_json TEXT,after_json TEXT,at TEXT)")
            for row in csv.DictReader(io.StringIO(inputs["tickets.csv"])):
                conn.execute("INSERT INTO tickets VALUES (?,?,?,?,?,?,?,?,?)",tuple(row[f] for f in TICKET_FIELDS))
            for row in csv.DictReader(io.StringIO(inputs["directory.csv"])):
                conn.execute("INSERT INTO directory VALUES (?,?,?)",(row["person"],row["team"],row["active"]=="true"))

    @contextmanager
    def connect(self):
        metadata=owned_run(self.run)
        contained(self.run,"resources/tickets.sqlite")
        if not self.path.is_file():raise ResourceError("Owned service database is missing")
        conn=None
        try:
            conn=sqlite3.connect(self.path)
            conn.row_factory=sqlite3.Row
            if conn.execute("PRAGMA application_id").fetchone()[0]!=APPLICATION_ID:
                raise ResourceError("Service database application identity mismatch")
            owner=conn.execute("SELECT run_id,task_id,workspace FROM owner").fetchall()
            if len(owner)!=1 or tuple(owner[0])!=(self.run.name,metadata["task_id"],str(core.ROOT)):
                raise ResourceError("Service database owner mismatch")
            with conn:yield conn
        except sqlite3.Error as exc:raise ResourceError("Service database error: "+str(exc)) from exc
        finally:
            if conn is not None:conn.close()

    def snapshot(self):
        with self.connect() as conn:
            return {"tickets":[dict(r) for r in conn.execute("SELECT * FROM tickets ORDER BY ticket_id")],
                    "directory":[dict(r) for r in conn.execute("SELECT * FROM directory ORDER BY person")],
                    "audit":[dict(r) for r in conn.execute("SELECT * FROM audit ORDER BY seq")]}

    def call(self,name,args):
        validate_arguments(name,args,"local_tickets")
        if name not in ARGUMENTS:raise ValueError("Not a service tool")
        with self.connect() as conn:
            if name not in ("tickets_list","tickets_get","directory_list"):conn.execute("BEGIN IMMEDIATE")
            if name=="tickets_list":return [dict(r) for r in conn.execute("SELECT * FROM tickets ORDER BY ticket_id")]
            if name=="directory_list":return [dict(r) for r in conn.execute("SELECT person,team,active FROM directory ORDER BY person")]
            row=conn.execute("SELECT * FROM tickets WHERE ticket_id=?",(args["ticket_id"],)).fetchone()
            if row is None:raise ValueError("Unknown ticket")
            before=dict(row)
            if name=="tickets_get":return before
            if row["status"]=="closed":raise ValueError("Closed tickets are read-only")
            after=dict(before)
            if name=="tickets_triage":
                if row["status"]!="open":raise ValueError("Only open tickets can be triaged")
                after["status"]="triaged"
            elif name=="tickets_assign":
                if row["status"]!="triaged":raise ValueError("Triage is required before assignment")
                person=conn.execute("SELECT * FROM directory WHERE person=? AND team=? AND active=1",(args["assignee"],args["team"])).fetchone()
                if person is None:raise ValueError("Assignee is not an active member of the selected team")
                due=datetime.fromisoformat(args["due_at"])
                if due.tzinfo is None:raise ValueError("Due timestamp requires an explicit timezone")
                after.update(status="assigned",team=args["team"],assignee=args["assignee"],due_at=args["due_at"])
            elif name=="tickets_request_approval":
                if row["status"]!="assigned":raise ValueError("Assignment is required before approval request")
                after["status"]="waiting_approval"
            conn.execute("UPDATE tickets SET status=?,team=?,assignee=?,due_at=? WHERE ticket_id=?",
                         (after["status"],after["team"],after["assignee"],after["due_at"],row["ticket_id"]))
            conn.execute("INSERT INTO audit(ticket_id,operation,before_json,after_json,at) VALUES (?,?,?,?,?)",
                         (row["ticket_id"],name,json.dumps(before,sort_keys=True),json.dumps(after,sort_keys=True),now()))
            return after


class TicketTools:
    def __init__(self,run,trace):
        self.trace=trace
        self.store=TicketStore(run)
        self.files=FileTools(run/"workspace",trace)

    def call(self,name,args):
        if not isinstance(name,str) or name not in ARGUMENTS:return self.files.call(name,args)
        event={"type":"tool_call","tool":name,"arguments":deepcopy(args),"started_at":now(),"is_model_output":False}
        self.trace.append(event)
        try:
            result=self.store.call(name,args)
            event.update(ended_at=now(),response=deepcopy(result))
            return result
        except Exception as exc:
            event.update(ended_at=now(),error=type(exc).__name__+": "+str(exc))
            raise
