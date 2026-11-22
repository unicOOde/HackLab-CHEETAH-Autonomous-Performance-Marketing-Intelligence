// Graph of graph_build_proposal.md. Idempotent (MERGE): safe to re-run.
// Every node carries proposal:'graph_build' so the whole graph can be removed with:
//   MATCH (n {proposal:'graph_build'}) DETACH DELETE n
// Properties with source:'inferred' are not stated in the proposal and were filled in.

// ---------- Components (graphwalk machinery) ----------
UNWIND [
  {name:'MetricGraph',  role:'metrics with compute + decomposition; formula edges derived automatically'},
  {name:'FactStore',    role:'DuckDB, hourly sums of columns per dimension'},
  {name:'Baseline',     role:'expected value per metric/slice; trailing 7-day weekday-adjusted median until 3 weeks of history exist'},
  {name:'Monitor',      role:'z-scores metrics against baseline, priority from recompute_importance()'},
  {name:'Investigator', role:'walks the graph, scores hypothesis nodes, updates causal weights online'},
  {name:'extend.py',    role:'onboards new metrics via lag correlation + LLM semantics; validates formula residuals'},
  {name:'ReplaySource', role:'sim/replay.py: streams OCT26 rows in conversion-time order into the FactStore'},
  {name:'IngestAdapter',role:'ingest/affiliate.py: CSV -> conversions + coverage tables, dimension profile'}
] AS c
MERGE (n:Component {name:c.name}) SET n.role = c.role, n.proposal = 'graph_build';

MATCH (ing:Component {name:'IngestAdapter'}), (fs:Component {name:'FactStore'}),
      (mg:Component {name:'MetricGraph'}), (bl:Component {name:'Baseline'}),
      (mon:Component {name:'Monitor'}), (inv:Component {name:'Investigator'}),
      (ext:Component {name:'extend.py'}), (rep:Component {name:'ReplaySource'})
MERGE (ing)-[:WRITES_TO]->(fs)
MERGE (rep)-[:FEEDS]->(fs)
MERGE (mg)-[:READS_FROM]->(fs)
MERGE (mon)-[:WATCHES]->(mg)
MERGE (mon)-[:COMPARES_AGAINST]->(bl)
MERGE (mon)-[:TRIGGERS]->(inv)
MERGE (inv)-[:WALKS]->(mg)
MERGE (ext)-[:ONBOARDS_INTO]->(mg);

// ---------- Tables ----------
UNWIND [
  {name:'conversions', grain:'hour x country x placement x vertical x category x type'},
  {name:'coverage',    grain:'hour x country x placement'}
] AS t
MERGE (n:Table {name:t.name}) SET n.grain = t.grain, n.proposal = 'graph_build'
WITH n
MATCH (fs:Component {name:'FactStore'})
MERGE (fs)-[:STORES]->(n);

// ---------- Columns (additive indicator columns) ----------
UNWIND [
  {name:'conv',           tbl:'conversions', value:'1',                                          entity:'E5'},
  {name:'sales',          tbl:'conversions', value:'1 if sale',                                  entity:'E6,E7,E20'},
  {name:'leads',          tbl:'conversions', value:'1 if lead',                                  entity:'E6,E7,E20'},
  {name:'amount',         tbl:'conversions', value:'raw',                                        entity:'E1-E4'},
  {name:'commission',     tbl:'conversions', value:'raw',                                        entity:'E1-E4'},
  {name:'payout',         tbl:'conversions', value:'raw',                                        entity:'E1-E4'},
  {name:'margin',         tbl:'conversions', value:'raw',                                        entity:'E1-E4'},
  {name:'sale_amount',    tbl:'conversions', value:'amount if sale',                             entity:'E8'},
  {name:'lag_h',          tbl:'conversions', value:'click->conversion hours',                    entity:'mean lag'},
  {name:'log_lag',        tbl:'conversions', value:'ln(1 + lag_s)',                              entity:'robust mean lag'},
  {name:'instant',        tbl:'conversions', value:'lag < 60 s',                                 entity:'B1'},
  {name:'subsec',         tbl:'conversions', value:'lag < 1 s',                                  entity:'B3'},
  {name:'late',           tbl:'conversions', value:'lag > 7 d',                                  entity:'D4'},
  {name:'batch',          tbl:'conversions', value:'Time in 00:00:00-00:00:10',                  entity:'D1'},
  {name:'zero_amt',       tbl:'conversions', value:'amount = 0',                                 entity:'D5'},
  {name:'zero_comm',      tbl:'conversions', value:'commission = 0',                             entity:'D6'},
  {name:'high_value',     tbl:'conversions', value:'sale and amount > rolling p90',              entity:'B5'},
  {name:'first_of_click', tbl:'conversions', value:'1 for first tx of (click time, placement, country)', entity:'E18'},
  {name:'id_span',        tbl:'conversions', value:'ID delta to previous tx',                    entity:'D7'},
  {name:'expected_active',tbl:'coverage',    value:'1 if pair converted in >= k of previous 14 days', entity:'breadth'},
  {name:'active',         tbl:'coverage',    value:'1 if pair converted in this bucket',         entity:'breadth'}
] AS c
MERGE (n:Column {name:c.name, table:c.tbl})
SET n.value = c.value, n.entity = c.entity, n.proposal = 'graph_build'
WITH n, c
MATCH (t:Table {name:c.tbl})
MERGE (n)-[:IN_TABLE]->(t);

// ---------- Dimensions ----------
UNWIND [
  {name:'hour',          kind:'time',     note:'bucketed by GRAIN (6 h or 1 day); outcomes by conversion Time, cohorts by Click Time'},
  {name:'country',       kind:'profiled', note:'cardinality < 200'},
  {name:'placement',     kind:'profiled', note:'cardinality < 200'},
  {name:'vertical',      kind:'profiled', note:'parent of placement (functional dependency, 0 violations)'},
  {name:'category',      kind:'profiled', note:'parent of placement; NOT a child of vertical'},
  {name:'type',          kind:'profiled', note:'cardinality < 200'},
  {name:'region',        kind:'derived',  note:'from country'},
  {name:'legacy',        kind:'derived',  note:'from "(Old)" suffix'},
  {name:'lag_bucket',    kind:'derived',  note:'from lag'},
  {name:'amount_bucket', kind:'derived',  note:'from amount'}
] AS d
MERGE (n:Dimension {name:d.name}) SET n.kind = d.kind, n.note = d.note, n.proposal = 'graph_build';

UNWIND [
  ['conversions','hour'],['conversions','country'],['conversions','placement'],
  ['conversions','vertical'],['conversions','category'],['conversions','type'],
  ['coverage','hour'],['coverage','country'],['coverage','placement']
] AS p
MATCH (t:Table {name:p[0]}), (d:Dimension {name:p[1]})
MERGE (t)-[:HAS_DIMENSION]->(d);

MATCH (pl:Dimension {name:'placement'}), (v:Dimension {name:'vertical'}), (cat:Dimension {name:'category'}),
      (co:Dimension {name:'country'}), (r:Dimension {name:'region'}), (lg:Dimension {name:'legacy'}),
      (lb:Dimension {name:'lag_bucket'}), (ab:Dimension {name:'amount_bucket'}),
      (lag:Column {name:'lag_h'}), (amt:Column {name:'amount'})
MERGE (pl)-[:ROLLS_UP_TO {basis:'functional dependency'}]->(v)
MERGE (pl)-[:ROLLS_UP_TO {basis:'functional dependency'}]->(cat)
MERGE (r)-[:DERIVED_FROM]->(co)
MERGE (lg)-[:DERIVED_FROM {source:'inferred'}]->(pl)
MERGE (lb)-[:DERIVED_FROM]->(lag)
MERGE (ab)-[:DERIVED_FROM]->(amt);

// ---------- Metrics (entity registry) ----------
UNWIND [
  {name:'margin',           compute:'margin',                    decomposition:'commission * (1 - payout_share)', north_star:true},
  {name:'commission',       compute:'commission',                decomposition:'gmv * comm_rate'},
  {name:'gmv',              compute:'amount',                    decomposition:'sales * aov'},
  {name:'sales',            compute:'sales',                     decomposition:'active_offers * sales_per_offer'},
  {name:'aov',              compute:'sale_amount / sales'},
  {name:'comm_rate',        compute:'commission / amount'},
  {name:'payout_share',     compute:'payout / commission'},
  {name:'sales_per_offer',  compute:'sales / active',            source:'inferred'},
  {name:'conv',             compute:'conv'},
  {name:'leads',            compute:'leads'},
  {name:'instant_share',    compute:'instant / conv'},
  {name:'subsec_share',     compute:'subsec / conv',             source:'inferred'},
  {name:'late_share',       compute:'late / conv',               source:'inferred'},
  {name:'batch_share',      compute:'batch / conv',              source:'inferred'},
  {name:'zero_amt_share',   compute:'zero_amt / conv',           source:'inferred'},
  {name:'zero_comm_share',  compute:'zero_comm / conv',          source:'inferred'},
  {name:'high_value_share', compute:'high_value / sales',        source:'inferred'},
  {name:'multi_conv',       compute:'1 - first_of_click / conv'},
  {name:'mean_lag',         compute:'lag_h / conv'},
  {name:'geo_mean_lag',     compute:'log_lag / conv',            source:'inferred'},
  {name:'network_share',    compute:'conv / id_span'},
  {name:'active_offers',    compute:'active',                    sliced_by:'nothing'},
  {name:'offer_breadth',    compute:'active / expected_active',  sliced_by:'placement'},
  {name:'country_breadth',  compute:'active / expected_active',  sliced_by:'country'}
] AS m
MERGE (n:Metric {name:m.name})
SET n.compute = m.compute, n.decomposition = m.decomposition,
    n.north_star = coalesce(m.north_star, false), n.source = coalesce(m.source, 'proposal'),
    n.proposal = 'graph_build'
WITH n
MATCH (mg:Component {name:'MetricGraph'})
MERGE (mg)-[:CONTAINS]->(n);

MATCH (mg:Component {name:'MetricGraph'}), (m:Metric {name:'margin'})
MERGE (mg)-[:NORTH_STAR]->(m);

// Metric -> columns it sums over (parsed from compute)
UNWIND [
  ['margin',['margin']],['commission',['commission']],['gmv',['amount']],['sales',['sales']],
  ['aov',['sale_amount','sales']],['comm_rate',['commission','amount']],['payout_share',['payout','commission']],
  ['sales_per_offer',['sales','active']],['conv',['conv']],['leads',['leads']],
  ['instant_share',['instant','conv']],['subsec_share',['subsec','conv']],['late_share',['late','conv']],
  ['batch_share',['batch','conv']],['zero_amt_share',['zero_amt','conv']],['zero_comm_share',['zero_comm','conv']],
  ['high_value_share',['high_value','sales']],['multi_conv',['first_of_click','conv']],
  ['mean_lag',['lag_h','conv']],['geo_mean_lag',['log_lag','conv']],['network_share',['conv','id_span']],
  ['active_offers',['active']],['offer_breadth',['active','expected_active']],['country_breadth',['active','expected_active']]
] AS r
UNWIND r[1] AS col
MATCH (m:Metric {name:r[0]}), (c:Column {name:col})
MERGE (m)-[:COMPUTED_FROM]->(c);

MATCH (m:Metric {name:'offer_breadth'}), (d:Dimension {name:'placement'}) MERGE (m)-[:SLICED_BY]->(d);
MATCH (m:Metric {name:'country_breadth'}), (d:Dimension {name:'country'}) MERGE (m)-[:SLICED_BY]->(d);

// ---------- Formula edges (from decompositions, weight 1.0) ----------
UNWIND [
  ['margin','commission'],['margin','payout_share'],
  ['commission','gmv'],['commission','comm_rate'],
  ['gmv','sales'],['gmv','aov'],
  ['sales','active_offers'],['sales','sales_per_offer']
] AS e
MATCH (p:Metric {name:e[0]}), (c:Metric {name:e[1]})
MERGE (p)-[r:FORMULA]->(c) SET r.weight = 1.0, r.status = 'validated';

// ---------- Causal edges (seeded priors, start as hypothesis) ----------
UNWIND [
  {from:'instant_share', to:'sales', source:'proposal'},
  {from:'offer_breadth', to:'sales', source:'proposal'},
  {from:'offer_breadth', to:'active_offers', source:'inferred'},
  {from:'country_breadth', to:'conv', source:'inferred'},
  {from:'conv', to:'sales', source:'inferred'},
  {from:'conv', to:'leads', source:'inferred'},
  {from:'mean_lag', to:'late_share', source:'inferred'},
  {from:'geo_mean_lag', to:'mean_lag', source:'inferred'},
  {from:'subsec_share', to:'instant_share', source:'inferred'},
  {from:'zero_amt_share', to:'aov', source:'inferred'},
  {from:'zero_comm_share', to:'comm_rate', source:'inferred'},
  {from:'high_value_share', to:'aov', source:'inferred'},
  {from:'batch_share', to:'conv', source:'inferred'},
  {from:'network_share', to:'conv', source:'inferred'},
  {from:'multi_conv', to:'conv', source:'inferred'}
] AS e
MATCH (a:Metric {name:e.from}), (b:Metric {name:e.to})
MERGE (a)-[r:CAUSAL]->(b)
SET r.status = 'hypothesis', r.alpha = 0.2, r.source = e.source;

// ---------- Hypothesis / signature nodes ----------
UNWIND [
  {name:'H_isp_outage',       desc:'ISP outage in a country'},
  {name:'H_payment_fail',     desc:'payment provider failing: sales drop, leads hold'},
  {name:'H_landing_slow',     desc:'slow landing page stretches click->conversion lag'},
  {name:'H_tracking_loss',    desc:'tracking pixel / postback lost for an offer'},
  {name:'H_advertiser_pause', desc:'advertiser paused offers'},
  {name:'H_release',          desc:'a release changed tracking or flow'},
  {name:'H_fraud',            desc:'fraudulent conversions'},
  {name:'H_data_incomplete',  desc:'recent buckets incomplete (lag CDF)'}
] AS h
MERGE (n:Hypothesis {name:h.name}) SET n.description = h.desc, n.proposal = 'graph_build'
WITH n
MATCH (inv:Component {name:'Investigator'})
MERGE (inv)-[:SCORES]->(n);

// Signature conditions: metric -[:SIGNALS {condition}]-> hypothesis
UNWIND [
  {m:'sales',            h:'H_payment_fail',     cond:'z < -3', source:'proposal'},
  {m:'leads',            h:'H_payment_fail',     cond:'z > -1', source:'proposal'},
  {m:'high_value_share', h:'H_payment_fail',     cond:'z < -2', source:'proposal'},
  {m:'multi_conv',       h:'H_payment_fail',     cond:'z > 2',  source:'proposal'},
  {m:'country_breadth',  h:'H_isp_outage',       cond:'z < -3 in one country', source:'inferred'},
  {m:'conv',             h:'H_isp_outage',       cond:'z < -3 in one country', source:'inferred'},
  {m:'mean_lag',         h:'H_landing_slow',     cond:'z > 3 for one offer',   source:'inferred'},
  {m:'geo_mean_lag',     h:'H_landing_slow',     cond:'z > 2 for one offer',   source:'inferred'},
  {m:'offer_breadth',    h:'H_tracking_loss',    cond:'one placement silent while leads elsewhere hold', source:'inferred'},
  {m:'active_offers',    h:'H_tracking_loss',    cond:'z < -2', source:'inferred'},
  {m:'active_offers',    h:'H_advertiser_pause', cond:'z < -3', source:'inferred'},
  {m:'offer_breadth',    h:'H_advertiser_pause', cond:'z < -3 across one vertical', source:'inferred'},
  {m:'zero_amt_share',   h:'H_release',          cond:'z > 3', source:'inferred'},
  {m:'zero_comm_share',  h:'H_release',          cond:'z > 3', source:'inferred'},
  {m:'instant_share',    h:'H_fraud',            cond:'z > 3', source:'inferred'},
  {m:'subsec_share',     h:'H_fraud',            cond:'z > 3', source:'inferred'},
  {m:'network_share',    h:'H_fraud',            cond:'z > 2', source:'inferred'},
  {m:'late_share',       h:'H_data_incomplete',  cond:'below lag-CDF expectation', source:'inferred'},
  {m:'batch_share',      h:'H_data_incomplete',  cond:'batch posting not yet arrived', source:'inferred'}
] AS s
MATCH (m:Metric {name:s.m}), (h:Hypothesis {name:s.h})
MERGE (m)-[r:SIGNALS]->(h) SET r.condition = s.cond, r.source = s.source;

// ---------- Replay scenarios (fault injection) ----------
UNWIND [
  {name:'drop_DE_12h',          desc:'drop all DE rows for 12 h',              h:'H_isp_outage',   m:null},
  {name:'drop_sales_keep_leads',desc:'drop sales but keep leads',              h:'H_payment_fail', m:null},
  {name:'silence_00008F',       desc:'silence placement 00008F',               h:'H_tracking_loss',m:null},
  {name:'stretch_lag_one_offer',desc:'stretch lags for one offer',             h:'H_landing_slow', m:null},
  {name:'weekend_dip',          desc:'innocent change: weekend dip',           h:null,             m:'conv'},
  {name:'batch_posting_burst',  desc:'innocent change: batch-posting burst',   h:null,             m:'batch_share'}
] AS s
MERGE (n:Scenario {name:s.name}) SET n.description = s.desc, n.innocent = s.h IS NULL, n.proposal = 'graph_build'
WITH n, s
MATCH (rep:Component {name:'ReplaySource'})
MERGE (rep)-[:INJECTS]->(n)
WITH n, s
OPTIONAL MATCH (h:Hypothesis {name:s.h})
OPTIONAL MATCH (m:Metric {name:s.m})
FOREACH (_ IN CASE WHEN h IS NULL THEN [] ELSE [1] END | MERGE (n)-[:SIMULATES]->(h))
FOREACH (_ IN CASE WHEN m IS NULL THEN [] ELSE [1] END | MERGE (n)-[:AFFECTS]->(m));

// ---------- Edge weights (section 4 of the proposal) ----------
// formula: exact, 1.0
MATCH ()-[r:FORMULA]->() SET r.weight = 1.0, r.weight_rule = 'formula: exact';

// causal: weight = prior x evidence. Proposal-seeded priors start higher than inferred ones;
// evidence starts at 1.0 (no investigation yet) and is updated online:
// w_new = (1 - alpha) * w_old + alpha * (explained_share * sign_agreement)
MATCH ()-[r:CAUSAL]->()
SET r.prior = CASE r.source WHEN 'proposal' THEN 0.7 ELSE 0.4 END,
    r.evidence = coalesce(r.evidence, 1.0),
    r.reject_below = 0.1,
    r.weight_rule = 'causal: prior x evidence, online update'
SET r.weight = r.prior * r.evidence;

// signature: each condition carries 1/n of its hypothesis, so
// score(H) = sum(weight of conditions that hold) is in 0..1
MATCH (h:Hypothesis)<-[r:SIGNALS]-()
WITH h, collect(r) AS rs
UNWIND rs AS r
SET r.weight = 1.0 / size(rs), r.holds = false,
    r.weight_rule = 'signature: 1/n of conditions, score = sum where holds';
MATCH (h:Hypothesis) SET h.score = 0.0;

// everything else is structural (deterministic): 1.0
MATCH ()-[r]->() WHERE NOT type(r) IN ['FORMULA','CAUSAL','SIGNALS']
SET r.weight = 1.0, r.weight_rule = 'structural: deterministic';
