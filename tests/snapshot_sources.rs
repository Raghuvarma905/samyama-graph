//! A `.sgsnap` header records where its data came from (#1911).
//!
//! `sources` is additive: a snapshot written without it reads unchanged, and a
//! header with none writes no `sources` key at all. When there are sources,
//! they travel with the data through import and export, two imports into one
//! graph keep both, and a graph changed after import says its sources may be
//! stale instead of claiming them silently.

use std::io::Read;

use samyama::graph::GraphStore;
use samyama::snapshot::format::{SnapshotHeader, SnapshotSource};

fn source(file: &str) -> SnapshotSource {
    SnapshotSource {
        format: "turtle".into(),
        file: file.into(),
        sha256: "ab".repeat(32),
        converter: "samyama-convert 1.12.0".into(),
        mapping: Some(serde_json::json!({ "preset": "skos" })),
    }
}

fn graph(label: &str) -> GraphStore {
    let mut s = GraphStore::new();
    let n = s.create_node(label);
    s.set_node_property("default", n, "name", label).unwrap();
    s
}

fn export(s: &GraphStore) -> Vec<u8> {
    let mut out = Vec::new();
    samyama::snapshot::export_tenant(s, &mut out).unwrap();
    out
}

fn header(bytes: &[u8]) -> SnapshotHeader {
    samyama::snapshot::peek_header(bytes).unwrap()
}

/// The header line as written, before any parsing fills in defaults.
fn header_json(bytes: &[u8]) -> serde_json::Map<String, serde_json::Value> {
    let mut text = String::new();
    flate2::read::GzDecoder::new(bytes)
        .read_to_string(&mut text)
        .unwrap();
    serde_json::from_str(text.lines().next().unwrap()).unwrap()
}

fn import(bytes: &[u8]) -> GraphStore {
    let mut s = GraphStore::new();
    samyama::snapshot::import_tenant(&mut s, bytes).unwrap();
    s
}

fn stale(h: &SnapshotHeader) -> bool {
    h.dropped.iter().any(|d| d.what == "sources_may_be_stale")
}

#[test]
fn a_header_with_no_sources_writes_no_sources_key() {
    let bytes = export(&graph("A"));
    assert!(
        !header_json(&bytes).contains_key("sources"),
        "{:?}",
        header_json(&bytes)
    );
    assert!(header(&bytes).sources.is_empty());
}

#[test]
fn a_header_written_before_sources_existed_reads_with_none() {
    // A v2 header as written before #1911: no `sources` key.
    let old = r#"{"format":"sgsnap","version":2,"tenant":"default","node_count":1,"edge_count":0,
        "labels":["A"],"edge_types":[],"created_at":"2026-10-01T00:00:00Z","samyama_version":"1.11.0",
        "dropped":[]}"#;
    let h: SnapshotHeader = serde_json::from_str(old).unwrap();
    assert!(h.sources.is_empty());
}

#[test]
fn sources_travel_with_the_data_through_import_and_export() {
    let mut s = graph("A");
    s.add_snapshot_sources([source("cso.ttl")]);
    let first = export(&s);
    assert_eq!(header(&first).sources, vec![source("cso.ttl")]);
    assert!(!stale(&header(&first)));

    // Imported and exported again, unchanged: the same sources, still current.
    let second = export(&import(&first));
    assert_eq!(header(&second).sources, vec![source("cso.ttl")]);
    assert!(!stale(&header(&second)), "{:?}", header(&second).dropped);
}

#[test]
fn two_imports_into_one_graph_keep_both_sources() {
    let mut a = graph("A");
    a.add_snapshot_sources([source("cso.ttl")]);
    let mut b = graph("B");
    b.add_snapshot_sources([source("acm-ccs.ttl")]);

    let mut merged = GraphStore::new();
    samyama::snapshot::import_tenant(&mut merged, export(&a).as_slice()).unwrap();
    samyama::snapshot::import_tenant(&mut merged, export(&b).as_slice()).unwrap();
    let h = header(&export(&merged));
    assert_eq!(h.sources, vec![source("cso.ttl"), source("acm-ccs.ttl")]);
    assert!(
        !stale(&h),
        "both imports are what the graph is: {:?}",
        h.dropped
    );
}

#[test]
fn a_graph_changed_after_import_flags_its_sources_as_possibly_stale() {
    let mut s = graph("A");
    s.add_snapshot_sources([source("cso.ttl")]);
    let mut restored = import(&export(&s));

    restored.create_node("Added");
    let h = header(&export(&restored));
    assert_eq!(h.sources, vec![source("cso.ttl")], "kept, not dropped");
    let flag = h
        .dropped
        .iter()
        .find(|d| d.what == "sources_may_be_stale")
        .expect("flagged");
    assert_eq!(flag.count, 1);
}

#[test]
fn a_failed_import_records_no_sources() {
    let mut s = graph("A");
    s.add_snapshot_sources([source("cso.ttl")]);
    let mut bytes = export(&s);
    bytes.truncate(bytes.len() / 2);

    let mut target = GraphStore::new();
    assert!(samyama::snapshot::import_tenant(&mut target, bytes.as_slice()).is_err());
    assert!(target.snapshot_sources().is_empty());
}

/// A snapshot exported as possibly stale stays flagged through another import
/// and export: importing it does not make it match its sources again (review
/// on #1940).
#[test]
fn a_stale_flag_survives_an_import_and_export_round_trip() {
    let mut s = graph("A");
    s.add_snapshot_sources([source("cso.ttl")]);
    s.create_node("Added");
    let first = export(&s);
    assert!(stale(&header(&first)), "{:?}", header(&first).dropped);

    let second = export(&import(&first));
    assert_eq!(header(&second).sources, vec![source("cso.ttl")]);
    assert!(stale(&header(&second)), "{:?}", header(&second).dropped);
}

/// Import A, change the graph, import B: A no longer describes the data, so
/// adding B does not make the pair current (review on #1940). Two imports with
/// no edit between them stay current, which
/// `two_imports_into_one_graph_keep_both_sources` pins.
#[test]
fn an_edit_between_two_imports_keeps_the_first_sources_stale() {
    let mut a = graph("A");
    a.add_snapshot_sources([source("cso.ttl")]);
    let mut b = graph("B");
    b.add_snapshot_sources([source("acm-ccs.ttl")]);

    let mut merged = GraphStore::new();
    samyama::snapshot::import_tenant(&mut merged, export(&a).as_slice()).unwrap();
    merged.create_node("Edited");
    samyama::snapshot::import_tenant(&mut merged, export(&b).as_slice()).unwrap();
    let h = header(&export(&merged));
    assert_eq!(h.sources, vec![source("cso.ttl"), source("acm-ccs.ttl")]);
    assert!(stale(&h), "{:?}", h.dropped);
}

/// The same rule through the store directly: sources added after an edit do
/// not make earlier, now-stale sources current.
#[test]
fn adding_sources_after_an_edit_keeps_the_earlier_ones_stale() {
    let mut s = graph("A");
    s.add_snapshot_sources([source("cso.ttl")]);
    s.create_node("Edited");
    s.add_snapshot_sources([source("acm-ccs.ttl")]);
    assert!(!s.snapshot_sources_are_current());
    assert!(stale(&header(&export(&s))));
}
