"""Materialize a public data map from previously completed business calls in one immutable epoch."""
import json
import re
import sqlite3

from routes import parse as route_query,rows as route_rows


IDENT=r"[A-Za-z_][A-Za-z_0-9]*"
CATALOG=re.compile(
    r"select\s+(?P<cols>(?:(?:name|sql|type)\s*,\s*)*(?:name|sql|type))\s+from\s+sqlite_(?:master|schema)"
    r"(?:\s+where\s+(?P<conditions>.+?))?(?:\s+order\s+by\s+(?P<sort>(?:name|type)(?:\s*,\s*(?:name|type))*))?",re.I)
ITEM=re.compile(r"type\s*=\s*'table'|type\s+in\s*\(\s*'table'\s*,\s*'view'\s*\)|name\s+not\s+like\s*'sqlite_%'",re.I)


def catalog_query(sql):
    match=CATALOG.fullmatch(sql.strip().removesuffix(";").strip())
    if not match:return None
    fields=[s.strip().lower() for s in match["cols"].split(",")]
    if not {"name","sql"}<=set(fields) or len(set(fields))!=len(fields):return None
    tests=re.split(r"\s+and\s+",match["conditions"] or "",flags=re.I)
    if tests!=[""] and not all(ITEM.fullmatch(s) for s in tests):return None
    compact=re.sub(r"\s+", "", " ".join(tests)).lower()
    kind="table" if "type=" in compact else "table_or_view" if "typein" in compact else "all"
    return fields,(kind,"user" if "notlike" in compact else "all_names")


def catalog_rows(key,observation):
    columns,_=key
    lines=observation.partition("\n\n")[2].splitlines()
    if not 2<len(lines)<52 or [s.strip().lower() for s in lines[0].split("|")]!=columns:
        return None
    if not re.fullmatch(r"(?:-+\+)*-+",lines[1].strip()):return None
    result={}
    for line in lines[2:]:
        data=[s.strip() for s in line.split("|")]
        if len(data)!=len(columns):return None
        r=dict(zip(columns,data))
        if r.get("type","table")!="table":return None
        name,ddl=r["name"],r["sql"]
        if not re.fullmatch(IDENT,name) or not re.match(rf"create\s+table\s+{re.escape(name)}\s*\(",ddl,re.I):return None
        if name in result:return None
        result[name]=ddl
    return result


def fields(name,ddl):
    # Parse the exposed DDL in an ephemeral in-memory database. No live connection is accepted.
    # sqlite_sequence is internal and unneeded. Exclude virtual tables, secondary statements and triggers.
    if name.lower().startswith("sqlite_"):return []
    with sqlite3.connect(":memory:") as db:
        try:
            db.execute(ddl)
            data=db.execute("SELECT name,type FROM pragma_table_info(?)",(name,)).fetchall()
        except sqlite3.Error:
            return []
    return [[n,typ] for n,typ in data if re.fullmatch(IDENT,n) and re.fullmatch(r"[A-Za-z0-9_ () ,]*",typ)]


class Notebook:
    def __init__(self):
        self.schema={}
        self.catalogs={}
        self.routes={}

    def observe(self,sql,observation,source):
        cat=catalog_query(sql)
        if cat is not None:
            raw=catalog_rows(cat,observation)
            if raw:
                if len(raw)>16 or sum(len(ddl) for ddl in raw.values())>12000:
                    return
                updates={table:(ddl,fields(table,ddl),source) for table,ddl in raw.items()}
                if any(table in self.schema and self.schema[table][0]!=ddl for table,(ddl,_,_) in updates.items()):
                    self.schema.clear();self.routes.clear();self.catalogs.clear()
                self.schema.update(updates)
                self.catalogs[cat[1]]=(sorted(raw),source)
            return
        q=route_query(sql)
        if q is None:return
        facts=route_rows(q,observation)
        if not facts:return
        for _,table in q.tables:
            labels=sorted({label for tab,label in facts if tab==table})
            if labels and all(0<len(label)<=80 and not any(ord(c)<32 for c in label) for label in labels):
                self.routes[(table,q.group)]=({s:facts[(table,s)] for s in labels},source)

    def payload(self):
        return {"fields":[{"table":table,"ddl":ddl,"columns":names,"from":source} for table,(ddl,names,source) in sorted(self.schema.items())],
                "catalogs":[{"type_scope":scope[0],"name_scope":scope[1],"seen":names,"from":source} for scope,(names,source) in sorted(self.catalogs.items())],
                "routes":[{"table":t,"group_column":c,"observed_group_counts":groups,"from":ref} for (t,c),(groups,ref) in sorted(self.routes.items())]}


def appendix(data):
    if not (data["fields"] or data["routes"]):return ""
    return ("\n\nPreviously observed data map for this read-only snapshot. The references are earlier QUERY calls in this same thread. "
            "Use covered fields and data sources for your next business query. Investigate missing facts with benchmark.act.\n"
            +json.dumps(data,ensure_ascii=False,sort_keys=True,separators=(",",":")))
