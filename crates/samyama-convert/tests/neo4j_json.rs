//! Neo4j `apoc.export.json` → `.sgsnap` (#1913).
//!
//! Both shapes APOC writes -- JSON Lines (its default) and the single object
//! from `{jsonFormat:'JSON'}` -- convert to a snapshot whose counts per label
//! and relationship type equal the file's, with every JSON value type kept as
//! the type it had. A relationship whose endpoint the file never defines is
//! handled by `--missing-endpoints`, never skipped quietly.

use std::collections::BTreeMap;
use std::path::Path;

use samyama::graph::{GraphStore, PropertyValue};
use samyama_convert::{convert, ConvertError, EndpointPolicy, Options};

/// Three people, two movies, four relationships; a string, integer, float,
/// boolean, list, map, and a string that only looks like a date. Ids are
/// written both quoted and bare, and one relationship comes before its nodes,
/// as APOC does not promise an order.
const JSONL: &str = r#"{"type":"relationship","id":"10","label":"ACTED_IN","start":{"id":"0","labels":["Person"]},"end":{"id":"3","labels":["Movie"]},"properties":{"roles":["Neo"],"paid":1.5}}
{"type":"node","id":"0","labels":["Person"],"properties":{"name":"Keanu","born":1964,"active":true}}
{"type":"node","id":"1","labels":["Person"],"properties":{"name":"Carrie-Anne","born":1967,"since":"1999-03-31"}}
{"type":"node","id":2,"labels":["Person","Director"],"properties":{"name":"Lana","meta":{"k":"v"}}}
{"type":"node","id":"3","labels":["Movie"],"properties":{"title":"The Matrix","rating":8.7}}
{"type":"node","id":"4","labels":["Movie"],"properties":{"title":"John Wick"}}
{"type":"relationship","id":"11","label":"ACTED_IN","start":{"id":"1"},"end":{"id":"3"},"properties":{}}
{"type":"relationship","id":"12","label":"DIRECTED","start":{"id":2},"end":{"id":"3"}}
{"type":"relationship","id":"13","label":"ACTED_IN","start":"0","end":"4","properties":{"roles":["John"]}}
"#;

/// The same graph in the single-object shape.
const OBJECT: &str = r#"{
  "nodes": [
    {"id":"0","labels":["Person"],"properties":{"name":"Keanu","born":1964,"active":true}},
    {"id":"1","labels":["Person"],"properties":{"name":"Carrie-Anne","born":1967,"since":"1999-03-31"}},
    {"id":"2","labels":["Person","Director"],"properties":{"name":"Lana","meta":{"k":"v"}}},
    {"id":"3","labels":["Movie"],"properties":{"title":"The Matrix","rating":8.7}},
    {"id":"4","labels":["Movie"],"properties":{"title":"John Wick"}}
  ],
  "rels": [
    {"id":"10","label":"ACTED_IN","start":{"id":"0"},"end":{"id":"3"},"properties":{"roles":["Neo"],"paid":1.5}},
    {"id":"11","label":"ACTED_IN","start":{"id":"1"},"end":{"id":"3"}},
    {"id":"12","label":"DIRECTED","start":{"id":"2"},"end":{"id":"3"}},
    {"id":"13","label":"ACTED_IN","start":{"id":"0"},"end":{"id":"4"},"properties":{"roles":["John"]}}
  ]
}"#;

/// `(nodes per label, relationships per type)` of a snapshot on disk.
fn counts(path: &Path) -> (BTreeMap<String, usize>, BTreeMap<String, usize>) {
    let mut s = GraphStore::new();
    samyama::snapshot::import_tenant(&mut s, std::fs::File::open(path).unwrap()).unwrap();
    let mut labels = BTreeMap::new();
    for n in s.all_nodes() {
        for l in &n.labels {
            *labels.entry(l.as_str().to_string()).or_default() += 1;
        }
    }
    let mut types = BTreeMap::new();
    for e in s.all_edges() {
        *types.entry(e.edge_type.as_str().to_string()).or_default() += 1;
    }
    (labels, types)
}

fn expected() -> (BTreeMap<String, usize>, BTreeMap<String, usize>) {
    let l = [("Director", 1), ("Movie", 2), ("Person", 3)];
    let t = [("ACTED_IN", 3), ("DIRECTED", 1)];
    (
        l.iter().map(|(k, v)| (k.to_string(), *v)).collect(),
        t.iter().map(|(k, v)| (k.to_string(), *v)).collect(),
    )
}

/// A node's properties as stored, keyed by name, read through
/// `node_materialized` so column-stored properties are included.
fn props_of(path: &Path, name: &str) -> BTreeMap<String, PropertyValue> {
    let mut s = GraphStore::new();
    samyama::snapshot::import_tenant(&mut s, std::fs::File::open(path).unwrap()).unwrap();
    s.all_nodes()
        .iter()
        .filter_map(|n| s.node_materialized(n.id))
        .find(|n| n.properties.get("name") == Some(&PropertyValue::String(name.into())))
        .map(|n| n.properties.into_iter().collect())
        .unwrap_or_else(|| panic!("no node named {name}"))
}

#[test]
fn both_apoc_shapes_convert_with_their_counts_and_value_types() {
    let dir = tempfile::tempdir().unwrap();
    for (name, text) in [("lines.json", JSONL), ("object.json", OBJECT)] {
        let input = dir.path().join(name);
        std::fs::write(&input, text).unwrap();
        let output = dir.path().join(format!("{name}.sgsnap"));

        let report = convert(&input, &output, &Options::default()).unwrap();
        assert_eq!(report.input.node_count(), 5, "{name}");
        assert_eq!(report.input.edge_count(), 4, "{name}");
        assert!(
            report.input.dropped().is_empty(),
            "{name}: {:?}",
            report.input.dropped()
        );
        assert_eq!(counts(&output), expected(), "{name}");

        let keanu = props_of(&output, "Keanu");
        assert_eq!(
            keanu.get("born"),
            Some(&PropertyValue::Integer(1964)),
            "{name}"
        );
        assert_eq!(
            keanu.get("active"),
            Some(&PropertyValue::Boolean(true)),
            "{name}"
        );
        // A date-shaped string stays a string: converting on sight would turn
        // identifiers and version numbers into dates.
        let carrie = props_of(&output, "Carrie-Anne");
        assert_eq!(
            carrie.get("since"),
            Some(&PropertyValue::String("1999-03-31".into())),
            "{name}"
        );
        let lana = props_of(&output, "Lana");
        assert!(
            matches!(lana.get("meta"), Some(PropertyValue::Map(_))),
            "{name}: {lana:?}"
        );
    }
}

/// One relationship to a node the file never defines, and one null property.
const DANGLING: &str = r#"{"type":"node","id":"0","labels":["Person"],"properties":{"name":"Keanu","nick":null}}
{"type":"relationship","id":"10","label":"KNOWS","start":{"id":"0"},"end":{"id":"99"}}
"#;

#[test]
fn a_dangling_relationship_fails_by_default_and_names_the_id() {
    let dir = tempfile::tempdir().unwrap();
    let input = dir.path().join("dangling.json");
    std::fs::write(&input, DANGLING).unwrap();
    let output = dir.path().join("out.sgsnap");

    match convert(&input, &output, &Options::default()) {
        Err(ConvertError::MissingEndpoints { count, first }) => {
            assert_eq!(count, 1);
            assert_eq!(first, vec!["99".to_string()]);
        }
        other => panic!("expected MissingEndpoints, got {other:?}"),
    }
    assert!(
        !output.exists(),
        "nothing is written when the input is refused"
    );
}

#[test]
fn drop_converts_and_reports_the_relationship_and_the_null() {
    let dir = tempfile::tempdir().unwrap();
    let input = dir.path().join("dangling.json");
    std::fs::write(&input, DANGLING).unwrap();
    let output = dir.path().join("out.sgsnap");
    let opts = Options {
        on_missing_endpoint: EndpointPolicy::Drop,
        ..Options::default()
    };

    let report = convert(&input, &output, &opts).unwrap();
    let what: Vec<&str> = report
        .input
        .dropped()
        .iter()
        .map(|d| d.what.as_str())
        .collect();
    assert_eq!(what, vec!["dangling_relationships", "null_properties"]);
    assert!(!report.lossless());
    assert_eq!(
        counts(&output).1.len(),
        0,
        "the dangling relationship is not in the output"
    );

    let strict = Options {
        require_lossless: true,
        ..opts
    };
    assert!(matches!(
        convert(&input, &dir.path().join("strict.sgsnap"), &strict),
        Err(ConvertError::Lossy(_))
    ));
}

#[test]
fn placeholder_keeps_the_relationship_against_an_unresolved_node() {
    let dir = tempfile::tempdir().unwrap();
    let input = dir.path().join("dangling.json");
    std::fs::write(&input, DANGLING).unwrap();
    let output = dir.path().join("out.sgsnap");
    let opts = Options {
        on_missing_endpoint: EndpointPolicy::Placeholder,
        ..Options::default()
    };

    let report = convert(&input, &output, &opts).unwrap();
    assert!(report
        .input
        .dropped()
        .iter()
        .any(|d| d.what == "unresolved_endpoints" && d.count == 1));
    let (labels, types) = counts(&output);
    assert_eq!(labels.get("Unresolved"), Some(&1));
    assert_eq!(types.get("KNOWS"), Some(&1));

    let mut s = GraphStore::new();
    samyama::snapshot::import_tenant(&mut s, std::fs::File::open(&output).unwrap()).unwrap();
    let held = s
        .all_nodes()
        .iter()
        .filter_map(|n| s.node_materialized(n.id))
        .find(|n| n.labels.iter().any(|l| l.as_str() == "Unresolved"))
        .and_then(|n| n.properties.get("neo4j_id").cloned());
    assert_eq!(held, Some(PropertyValue::String("99".into())));
}
