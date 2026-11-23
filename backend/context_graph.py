"""Read-only view of the Neo4j graph for the Context Explorer screen.

Only the fixed, parameterized Cypher in this file is ever run (read routing, 10 s timeout). A label
is interpolated into Cypher only after it passes the LABELS allow-list. One node at a time: its
properties, its neighbours grouped by (relationship, direction, neighbour label) and capped, and
panel extras (monthly totals, latest closed day, agent findings, recent changes).

Non-fabrication: when Neo4j isn't configured or a read fails, the result is an explicit
`unavailable` with a reason (never credentials) and no nodes.
"""
import graph_sync

# graph_store.BASE_LABELS | AGENT_LABELS, minus Change (audit entries only show in the panel)
LABELS={'Freecash','Ad','Country','Category','Vertical','ClickHour','LagBucket','TransactionType','Currency','Month',
        'Metric','Day','Hour','PlannedEvent','Dataset','Outage','Hypothesis','Signal'}
TIMEOUT_S=10
MAX_LIMIT=40
_W='coalesce(r.amount, r.commission, r.tx, r.revenue, 0)'


def _read(cypher, params):
    from neo4j import Query
    import os
    recs,_,_=graph_sync._get_driver().execute_query(Query(cypher,timeout=TIMEOUT_S),params,
                                                   database_=os.getenv('NEO4J_DATABASE') or None,routing_='r')
    return [r.data() for r in recs]


def _clean(v):
    if v is None or isinstance(v,(bool,int,float,str)):return v
    if isinstance(v,dict):return {str(k):_clean(x) for k,x in v.items()}
    if isinstance(v,(list,tuple)):return [_clean(x) for x in v]
    return str(v)  # neo4j temporal types etc.


def unavailable(reason, error_class=None):
    out={'status':'unavailable','reason':reason,'graph_version':None,'nodes':[],'edges':[]}
    if error_class:out['error_class']=error_class
    return out


def _center(label):
    if label not in LABELS:raise ValueError('label not allowed')
    return f'MATCH (n:`{label}`) WHERE n.id = $id OR toString(n.id) = $id WITH n LIMIT 1 '


Q_DEFAULT="OPTIONAL MATCH (f:Freecash) WITH f LIMIT 1 OPTIONAL MATCH (d:Dataset) WITH f, d LIMIT 1 RETURN toString(f.id) AS f, toString(d.id) AS d"
Q_NODE="RETURN properties(n) AS props"
Q_GROUPS=f"""MATCH (n)-[r]-(m) WHERE NOT m:Change
WITH type(r) AS rel, startNode(r) = n AS outgoing, labels(m)[0] AS ml, m, count(r) AS k, sum({_W}) AS w, collect(properties(r))[0] AS edge
ORDER BY w DESC, toString(m.id)
WITH rel, outgoing, ml, count(m) AS total, collect({{id: toString(m.id), name: m.name, edges: k, edge: edge}})[0..$limit] AS top
RETURN rel, outgoing, ml AS label, total, top ORDER BY total DESC"""
_SUMS='sum(r.tx) AS tx, sum(r.amount) AS amount, sum(r.commission) AS commission, sum(r.payout) AS payout, sum(r.margin) AS margin'
Q_TOTALS={
    'Ad':'MATCH (:Freecash)-[r:RUNS]->(n) RETURN '+_SUMS,
    'Freecash':'MATCH (n)-[r:RUNS]->(:Ad) RETURN count(r) AS ads, '+_SUMS,
    'Country':'MATCH (:Ad)-[r:CONVERTS_IN]->(n) RETURN count(r) AS ads, '+_SUMS,
    'Category':'MATCH (:Freecash)-[r:RUNS]->(:Ad)-[:IN_CATEGORY]->(n) RETURN count(r) AS ads, '+_SUMS,
    'Vertical':'MATCH (:Freecash)-[r:RUNS]->(:Ad)-[:IN_VERTICAL]->(n) RETURN count(r) AS ads, '+_SUMS,
}
_LATEST=' RETURN d.id AS day, properties(e) AS edge ORDER BY d.id DESC LIMIT 1'
Q_LATEST={
    'Ad':'MATCH (n)-[e:ON_DAY]->(d:Day)'+_LATEST,
    'Dataset':'MATCH (n)-[e:CLOSED_DAY]->(d:Day)'+_LATEST,
    'Freecash':'MATCH (:Dataset)-[e:CLOSED_DAY]->(d:Day) WITH n, e, d'+_LATEST,
    'Day':'MATCH (:Dataset)-[e:CLOSED_DAY]->(d:Day) WHERE d = n'+_LATEST,
}
Q_FINDINGS="""MATCH (f)-[]-(n) WHERE f:Hypothesis OR f:Signal OR f:Outage
RETURN DISTINCT labels(f)[0] AS label, toString(f.id) AS id, f.status AS status,
       coalesce(f.text, f.summary, f.issue_key, f.service) AS text, f.confidence AS confidence, f.severity AS severity LIMIT 20"""
Q_CHANGES="MATCH (c:Change)-[:CHANGED]->(n) RETURN c.agent AS agent, c.op AS op, c.ts AS ts, c.reason AS reason ORDER BY c.ts DESC LIMIT 5"
Q_SEARCH="""MATCH (n) WHERE NOT n:Change AND NOT n:Hour AND n.id IS NOT NULL
  AND (toLower(toString(n.id)) CONTAINS $q OR toLower(coalesce(toString(n.name), '')) CONTAINS $q)
RETURN labels(n)[0] AS label, toString(n.id) AS id, n.name AS name
ORDER BY CASE WHEN toLower(toString(n.id)) = $q THEN 0 ELSE 1 END, size(toString(n.id)), toString(n.id) LIMIT 40"""


def _one(rows):return _clean(rows[0]) if rows else None


def neighbourhood(label=None, node_id=None, limit=12):
    """Raises ValueError (bad label), LookupError (node not found) or a provider error."""
    limit=max(1,min(int(limit),MAX_LIMIT))
    if label is not None and label not in LABELS:raise ValueError('label not allowed')
    if not label:
        d=_read(Q_DEFAULT,{})[0]
        if d.get('f'):label,node_id='Freecash',d['f']
        elif d.get('d'):label,node_id='Dataset',d['d']
        else:raise LookupError('graph is empty')
    c=_center(label);p={'id':str(node_id)}
    rows=_read(c+Q_NODE,p)
    if not rows:raise LookupError('node not found')
    props=_clean(rows[0]['props'])
    groups=[]
    for g in _read(c+Q_GROUPS,{**p,'limit':limit}):
        if g['label'] not in LABELS:continue
        groups.append({'rel':g['rel'],'direction':'out' if g['outgoing'] else 'in','label':g['label'],'total':g['total'],
                       'top':[{**_clean(t),'label':g['label']} for t in g['top']]})
    panel={'totals':_one(_read(c+Q_TOTALS[label],p)) if label in Q_TOTALS else None,
           'latest_day':_one(_read(c+Q_LATEST[label],p)) if label in Q_LATEST else None,
           'findings':_clean(_read(c+Q_FINDINGS,p)),'changes':_clean(_read(c+Q_CHANGES,p)),
           'relationships':[{'rel':g['rel'],'direction':g['direction'],'label':g['label'],'total':g['total']} for g in groups]}
    center={'label':label,'id':str(props.get('id',node_id)),'name':props.get('name'),'props':props}
    nodes=[center]+[t for g in groups for t in g['top']]
    edges=[{'source':center['id'] if g['direction']=='out' else t['id'],'target':t['id'] if g['direction']=='out' else center['id'],'rel':g['rel']}
           for g in groups for t in g['top']]
    return {'status':'ok','graph_version':None,'center':center,'groups':groups,'panel':panel,
            'nodes':[{'label':n['label'],'id':n['id'],'name':n.get('name')} for n in nodes],'edges':edges,
            'shown':len(nodes)-1,'total':sum(g['total'] for g in groups)}


def search(q):
    q=(q or '').strip().lower()
    if not q:return {'status':'ok','results':[]}
    rows=[r for r in _clean(_read(Q_SEARCH,{'q':q})) if r['label'] in LABELS]
    return {'status':'ok','results':rows[:20]}
