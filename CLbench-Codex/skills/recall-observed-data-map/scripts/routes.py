"""Conservative development replay of grouped business-data routing, using public rows."""
import re
from dataclasses import dataclass


IDENT = r"[a-zA-Z_][a-zA-Z_0-9]*"
TAG = r"'([^']+)'(?:\s+(?:as\s+)?[a-zA-Z_][a-zA-Z_0-9]*)?"
ALIAS = rf"(?:\s+(?:as\s+)?{IDENT})?"
ARM = re.compile(rf"select\s+{TAG}\s*,\s*({IDENT}){ALIAS}\s*,\s*(.*?)\s+from\s+({IDENT})\s+group\s+by\s+({IDENT})",re.I)
AGGREGATE = re.compile(rf"(count|min|max|avg|sum)\(\s*(\*|{IDENT})\s*\){ALIAS}",re.I)
ORDER = re.compile(rf"\s+order\s+by\s+{IDENT}(?:\s+(?:asc|desc))?(?:\s*,\s*{IDENT}(?:\s+(?:asc|desc))?)*$",re.I)


@dataclass(frozen=True)
class Query:
    group: str
    tables: tuple
    width: int
    pure: bool


def parse(sql):
    sql=ORDER.sub("",sql.strip().removesuffix(";").strip())
    arms=re.split(r"\s+union\s+all\s+",sql,flags=re.I)
    if len(arms)<2:
        return None
    tables=[]
    grouped=width=None
    pure=True
    for arm in arms:
        match=ARM.fullmatch(arm)
        if not match:
            return None
        tag,column,metrics,table,group_by=match.groups()
        column,table=column.casefold(),table.casefold()
        expressions=[AGGREGATE.fullmatch(expr.strip()) for expr in metrics.split(",")]
        if (not all(expressions) or column!=group_by.casefold() or grouped not in (None,column)
            or width not in (None,len(expressions))):
            return None
        if tuple(x.casefold() for x in expressions[0].groups())!=("count","*"):
            return None
        grouped,width=column,len(expressions)
        tables.append((tag,table))
        pure=pure and width==1
    if len({tag for tag,_ in tables})!=len(tables) or len({table for _,table in tables})!=len(tables):
        return None
    return Query(grouped,tuple(tables),width+2,pure)


def rows(query,observation):
    section=observation.partition("\n\n")[2].splitlines()
    if (len(section)<3 or len(section)-2>=50 or len(section[0].split("|"))!=query.width
        or not re.fullmatch(r"(?:-+\+)*-+",section[1].strip())):
        return None
    table_for=dict(query.tables)
    parsed={}
    for line in section[2:]:
        parts=[x.strip() for x in line.split("|")]
        if len(parts)!=query.width or parts[0] not in table_for or not parts[1] or not parts[2].isdecimal():
            return None
        key=(table_for[parts[0]],parts[1])
        if key in parsed or int(parts[2])<=0:
            return None
        parsed[key]=int(parts[2])
    if {table for table,_ in parsed}!=set(table_for.values()):
        return None
    return parsed


class Routes:
    def __init__(self):
        self.tables={}

    def observe(self,query,observation,source):
        facts=rows(query,observation)
        if facts is None:
            return
        for _,table in query.tables:
            self.tables[(query.group,table)]=(source,{k:v for k,v in facts.items() if k[0]==table})

    def previous(self,query,case):
        if query is None or not query.pure:
            return None
        facts={}
        refs=[]
        for _,table in query.tables:
            entry=self.tables.get((query.group,table))
            if entry is None or entry[0][0]==case:
                return None
            refs.append(entry[0])
            facts.update(entry[1])
        return facts,sorted(set(refs))
