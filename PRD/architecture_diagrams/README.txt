CHEETAH Master PDR v2.0 -- Architecture drawing source package

Eight drawings are embedded in CHEETAH_Master_PDR_v2.0.docx.
Each drawing has three representations:
- SVG: scalable vector export for editing or embedding in slides
- PNG: high resolution raster export for documents
- DOT: editable Graphviz source (generate with dot -Tsvg file.dot -o file.svg)

01_system_context   User, six-screen UI, backend, database, graph, inference providers
02_runtime_components   Modular FastAPI + worker runtime and storage boundaries
03_daily_clock   October simulated daily close, deviation detection, anomaly pause
04_agent_workflow   Investigator, ReAct, Evidence Reviewer, graph and action governance
05_replay_branches   Learning Replay vs Fix Validation Replay
06_data_model   Source/ledger and run/forecast/incident/evaluation entity groups
07_frontend_screens   Six screens and common simulation toolbar
08_event_delivery   PostgreSQL outbox -> FastAPI SSE -> UI snapshot/reconcile

The diagrams are target-architecture proposals; deployment/completion status is tracked separately in the PDR.
