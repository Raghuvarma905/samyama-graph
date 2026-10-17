//! The point index: an R-tree over a property holding points (NDS-04, #1618).
//!
//! `CREATE POINT INDEX` was refused, so `point.withinBBox` and a
//! `point.distance(...) < r` bound ran as a scan of the label. Now the
//! planner answers both from the R-tree when the property has one, and
//! `db.index.point.nearest` answers k-nearest. Every check here compares
//! against the same query with no index -- a scan -- so the index can make a
//! query cheaper and is shown never to change its answer.

use samyama::graph::{GraphStore, Label, PropertyValue};
use samyama::query::executor::{MutQueryExecutor, QueryExecutor, Value};
use samyama::query::parser::parse_query;

/// A 30x30 grid of `:Site` with `loc` cartesian, and 200 `:City` with `at`
/// geographic around Bengaluru, ids in `id`.
fn store(indexed: bool) -> GraphStore {
    let mut s = GraphStore::new();
    let mut id = 0i64;
    for i in 0..30 {
        for j in 0..30 {
            run(&mut s, &format!("CREATE (:Site {{id: {id}, loc: point({{x: {}, y: {}}})}})", i as f64 * 2.0, j as f64));
            id += 1;
        }
    }
    for k in 0..200 {
        let lat = 12.9 + (k % 20) as f64 * 0.01;
        let lon = 77.5 + (k / 20) as f64 * 0.01;
        run(&mut s, &format!("CREATE (:City {{id: {k}, at: point({{latitude: {lat}, longitude: {lon}}})}})"));
    }
    if indexed {
        run(&mut s, "CREATE POINT INDEX site_loc FOR (n:Site) ON (n.loc)");
        run(&mut s, "CREATE POINT INDEX ON :City(at)");
    }
    s
}

fn run(s: &mut GraphStore, q: &str) {
    MutQueryExecutor::new(s, "default".into())
        .execute(&parse_query(q).unwrap_or_else(|e| panic!("{q}: {e}")))
        .unwrap_or_else(|e| panic!("{q}: {e}"));
}

fn ids(s: &GraphStore, q: &str) -> Vec<i64> {
    let b = QueryExecutor::new(s).execute(&parse_query(q).unwrap()).unwrap_or_else(|e| panic!("{q}: {e}"));
    let mut v: Vec<i64> = b
        .records
        .iter()
        .map(|r| match r.get("id") {
            Some(Value::Property(PropertyValue::Integer(i))) => *i,
            other => panic!("{other:?}"),
        })
        .collect();
    v.sort();
    v
}

fn plan(s: &GraphStore, q: &str) -> String {
    let b = QueryExecutor::new(s).execute(&parse_query(&format!("EXPLAIN {q}")).unwrap()).unwrap();
    format!("{:?}", b.records[0])
}

const QUERIES: &[&str] = &[
    "MATCH (n:Site) WHERE point.withinBBox(n.loc, point({x: 10, y: 5}), point({x: 20, y: 12})) RETURN n.id AS id",
    "MATCH (n:Site) WHERE point.distance(n.loc, point({x: 30, y: 15})) < 4.5 RETURN n.id AS id",
    "MATCH (n:Site) WHERE 3 >= point.distance(point({x: 0, y: 0}), n.loc) RETURN n.id AS id",
    "MATCH (n:City) WHERE point.distance(n.at, point({latitude: 12.95, longitude: 77.55})) < 2500 RETURN n.id AS id",
];

#[test]
fn the_planner_uses_the_index_and_the_answer_is_the_scan_s() {
    let (scan, index) = (store(false), store(true));
    for q in QUERIES {
        assert!(!plan(&scan, q).contains("PointIndexScan"), "{q}");
        assert!(plan(&index, q).contains("PointIndexScan"), "{q}: {}", plan(&index, q));
        let want = ids(&scan, q);
        assert!(!want.is_empty(), "{q}: the fixture matches nothing");
        assert_eq!(ids(&index, q), want, "{q}");
    }
}

#[test]
fn the_index_follows_writes() {
    let mut s = store(true);
    let q = "MATCH (n:Site) WHERE point.withinBBox(n.loc, point({x: 100, y: 100}), point({x: 101, y: 101})) RETURN n.id AS id";
    assert!(ids(&s, q).is_empty());
    run(&mut s, "MATCH (n:Site {id: 7}) SET n.loc = point({x: 100.5, y: 100.5})");
    assert_eq!(ids(&s, q), vec![7]);
    run(&mut s, "MATCH (n:Site {id: 7}) REMOVE n.loc");
    assert!(ids(&s, q).is_empty());
    run(&mut s, "CREATE (:Site {id: 9001, loc: point({x: 100.2, y: 100.9})})");
    assert_eq!(ids(&s, q), vec![9001]);
    run(&mut s, "MATCH (n:Site {id: 9001}) REMOVE n:Site");
    assert!(ids(&s, q).is_empty());
    run(&mut s, "MATCH (n {id: 9001}) SET n:Site");
    assert_eq!(ids(&s, q), vec![9001]);
    run(&mut s, "MATCH (n:Site {id: 9001}) DETACH DELETE n");
    assert!(ids(&s, q).is_empty());
}

#[test]
fn nearest_returns_the_k_closest_in_order() {
    let s = store(true);
    let b = QueryExecutor::new(&s)
        .execute(
            &parse_query(
                "CALL db.index.point.nearest('Site', 'loc', point({x: 11.1, y: 7.2}), 4) \
                 YIELD node, distance RETURN node.id AS id, distance",
            )
            .unwrap(),
        )
        .unwrap();
    let got: Vec<f64> = b
        .records
        .iter()
        .map(|r| match r.get("distance") {
            Some(Value::Property(PropertyValue::Float(d))) => *d,
            other => panic!("{other:?}"),
        })
        .collect();
    assert_eq!(got.len(), 4);
    assert!(got.windows(2).all(|w| w[0] <= w[1]), "{got:?}");
    // The nearest grid point to (11.1, 7.2) is (12, 7), 0.92 away.
    assert!((got[0] - ((0.9f64).powi(2) + (0.2f64).powi(2)).sqrt()).abs() < 1e-9, "{got:?}");

    let geo = QueryExecutor::new(&s)
        .execute(
            &parse_query(
                "CALL db.index.point.nearest('City', 'at', point({latitude: 12.9, longitude: 77.5}), 3) \
                 YIELD node, distance RETURN distance",
            )
            .unwrap(),
        )
        .unwrap();
    // The first is the city at the query point; the next two ~1.1 km away (0.01 degrees).
    let d: Vec<f64> = geo
        .records
        .iter()
        .map(|r| match r.get("distance") {
            Some(Value::Property(PropertyValue::Float(d))) => *d,
            other => panic!("{other:?}"),
        })
        .collect();
    assert!(d[0] < 1.0 && (1000.0..1200.0).contains(&d[1]), "{d:?}");
}

#[test]
fn drop_removes_it_and_the_catalog_carries_it() {
    let mut s = store(true);
    let defs = format!("{:?}", s.index_catalog().definitions);
    assert!(defs.contains("Point") && defs.contains("site_loc"), "{defs}");
    run(&mut s, "DROP POINT INDEX site_loc");
    assert!(!plan(&s, QUERIES[0]).contains("PointIndexScan"));
    assert!(!format!("{:?}", s.index_catalog().definitions).contains("site_loc"));

    // Restored from a catalog, the index answers as it did.
    let mut fresh = store(false);
    let cat = store(true).index_catalog();
    let report = fresh.restore_index_catalog(&cat);
    assert_eq!(report.point, 2);
    assert!(plan(&fresh, QUERIES[1]).contains("PointIndexScan"));
    assert_eq!(ids(&fresh, QUERIES[1]), ids(&store(false), QUERIES[1]));

    let err = MutQueryExecutor::new(&mut s, "default".into())
        .execute(&parse_query("DROP POINT INDEX nope").unwrap())
        .unwrap_err()
        .to_string();
    assert!(err.contains("no point index named 'nope'"), "{err}");
}

#[test]
fn nearest_without_an_index_is_an_error() {
    let s = store(false);
    let err = QueryExecutor::new(&s)
        .execute(&parse_query("CALL db.index.point.nearest('Site', 'loc', point({x: 0, y: 0}), 1) YIELD node RETURN node").unwrap())
        .unwrap_err()
        .to_string();
    assert!(err.contains("no point index on :Site(loc)"), "{err}");
    let _ = Label::new("unused");
}

#[test]
fn index_memory_reports_each_point_index() {
    // The index memory report covers every index kind (#1673); the point
    // index was added after it and has to appear there too.
    let s = store(true);
    let points: Vec<_> = s.index_memory().into_iter().filter(|m| m.kind == "point").collect();
    assert_eq!(points.len(), 2, "{points:?}");
    let site = points.iter().find(|m| m.name.as_deref() == Some("site_loc")).expect("site_loc");
    assert_eq!((site.label.as_str(), site.property.as_str(), site.entries), ("Site", "loc", 900));
    assert!(site.bytes > 0);
    let city = points.iter().find(|m| m.label == "City").expect("City index");
    assert_eq!((city.property.as_str(), city.entries), ("at", 200));
}
