/* FLOW_STEWARD_AGE_RESTORE_REPAIR */
DO $flow_steward$
DECLARE
    stale_graphs bigint;
BEGIN
    IF to_regclass('ag_catalog.ag_graph') IS NULL
       OR to_regclass('ag_catalog.ag_label') IS NULL THEN
        RETURN;
    END IF;
    EXECUTE 'LOCK TABLE ag_catalog.ag_graph IN ACCESS EXCLUSIVE MODE';
    EXECUTE 'LOCK TABLE ag_catalog.ag_label IN ACCESS EXCLUSIVE MODE';
    EXECUTE 'SELECT count(*) FROM ag_catalog.ag_graph '
            'WHERE graphid IS DISTINCT FROM namespace::oid'
       INTO stale_graphs;
    IF stale_graphs = 0 THEN
        RETURN;
    END IF;
    EXECUTE 'ALTER TABLE ag_catalog.ag_label DROP CONSTRAINT fk_graph_oid';
    EXECUTE 'UPDATE ag_catalog.ag_label AS label SET graph = graph.namespace::oid '
            'FROM ag_catalog.ag_graph AS graph '
            'WHERE label.graph = graph.graphid '
            'AND graph.graphid IS DISTINCT FROM graph.namespace::oid';
    EXECUTE 'UPDATE ag_catalog.ag_graph SET graphid = namespace::oid '
            'WHERE graphid IS DISTINCT FROM namespace::oid';
    EXECUTE 'ALTER TABLE ag_catalog.ag_label ADD CONSTRAINT fk_graph_oid '
            'FOREIGN KEY (graph) REFERENCES ag_catalog.ag_graph (graphid)';
    EXECUTE 'SELECT count(*) FROM ag_catalog.ag_graph '
            'WHERE graphid IS DISTINCT FROM namespace::oid'
       INTO stale_graphs;
    IF stale_graphs <> 0 THEN
        RAISE EXCEPTION 'AGE graph catalog identity repair did not converge';
    END IF;
END
$flow_steward$;
