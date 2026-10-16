//! Operators over the point index (NDS-04, #1618): its DDL, the scan the
//! planner picks for a spatial predicate, and `db.index.point.nearest`.

use crate::graph::{GraphStore, Label, NodeId, PropertyValue};
use crate::index::point::{geographic_box, point_coords, Rect};
use crate::query::ast::{BinaryOp, Expression};
use crate::query::executor::operator::{eval_expression, OperatorDescription, PhysicalOperator};
use crate::query::executor::{ExecutionError, ExecutionResult, Record, Value};

/// `CREATE POINT INDEX` / `DROP POINT INDEX`.
pub struct PointIndexDdlOperator {
    ddl: crate::query::ast::PointIndexDdl,
    executed: bool,
}

impl PointIndexDdlOperator {
    pub fn new(ddl: crate::query::ast::PointIndexDdl) -> Self {
        Self { ddl, executed: false }
    }
}

impl PhysicalOperator for PointIndexDdlOperator {
    fn next(&mut self, _store: &GraphStore) -> ExecutionResult<Option<Record>> {
        Err(ExecutionError::RuntimeError("point index DDL is a write; it needs next_mut".to_string()))
    }

    fn next_mut(&mut self, store: &mut GraphStore, _tenant_id: &str) -> ExecutionResult<Option<Record>> {
        use crate::query::ast::PointIndexDdl;
        if self.executed {
            return Ok(None);
        }
        self.executed = true;
        match &self.ddl {
            PointIndexDdl::Create { name, label, property } => {
                store.create_point_index(name, label.as_str(), property);
            }
            PointIndexDdl::Drop { name } => {
                if !store.drop_point_index(name) {
                    let known: Vec<String> = store.point_index.listing().into_iter().map(|(n, ..)| n).collect();
                    return Err(ExecutionError::RuntimeError(format!(
                        "no point index named '{name}'. Known point indexes: {}",
                        if known.is_empty() { "none".to_string() } else { known.join(", ") }
                    )));
                }
            }
        }
        Ok(Some(Record::new()))
    }

    fn reset(&mut self) {
        self.executed = false;
    }

    fn is_mutating(&self) -> bool {
        true
    }

    fn describe(&self) -> OperatorDescription {
        use crate::query::ast::PointIndexDdl;
        let (name, details) = match &self.ddl {
            PointIndexDdl::Create { name, label, property } => {
                ("CreatePointIndex", format!("{name} ON :{}({property})", label.as_str()))
            }
            PointIndexDdl::Drop { name } => ("DropPointIndex", name.clone()),
        };
        OperatorDescription { name: name.to_string(), details, children: Vec::new() }
    }
}

/// A region a spatial predicate confines `var.property` to, in the index's
/// coordinates, read from a predicate the planner found. Returns `None` for
/// anything it does not recognise, which leaves the scan to the planner's
/// other choices. The predicate stays in the filter either way.
pub fn spatial_region(var: &str, pred: &Expression, store: &GraphStore) -> Option<(String, Rect)> {
    let constant_point = |e: &Expression| -> Option<(f64, f64, bool)> {
        let mut vars = std::collections::HashSet::new();
        crate::query::executor::planner::QueryPlanner::collect_vars(e, &mut vars);
        if !vars.is_empty() {
            return None;
        }
        match eval_expression(e, &Record::new(), store).ok()? {
            Value::Property(v) => point_coords(&v),
            _ => None,
        }
    };
    let constant_number = |e: &Expression| -> Option<f64> {
        let mut vars = std::collections::HashSet::new();
        crate::query::executor::planner::QueryPlanner::collect_vars(e, &mut vars);
        if !vars.is_empty() {
            return None;
        }
        match eval_expression(e, &Record::new(), store).ok()? {
            Value::Property(PropertyValue::Float(f)) => Some(f),
            Value::Property(PropertyValue::Integer(i)) => Some(i as f64),
            _ => None,
        }
    };
    let property_of_var = |e: &Expression| -> Option<String> {
        match e {
            Expression::Property { variable, property } if variable == var => Some(property.clone()),
            _ => None,
        }
    };
    match pred {
        // point.withinBBox(n.p, lo, hi)
        Expression::Function { name, args, .. } if name.eq_ignore_ascii_case("point.withinBBox") && args.len() == 3 => {
            let prop = property_of_var(&args[0])?;
            let (lx, ly, lg) = constant_point(&args[1])?;
            let (hx, hy, hg) = constant_point(&args[2])?;
            // A box across the antimeridian (lo east of hi) is two boxes; the
            // scan declines it rather than answering half.
            if lg != hg || lx > hx || ly > hy {
                return None;
            }
            Some((prop, Rect { min_x: lx, min_y: ly, max_x: hx, max_y: hy }))
        }
        // point.distance(n.p, c) < r, <= r, and the mirrored r > / >= distance.
        Expression::Binary { left, op, right } => {
            let (dist, bound) = match op {
                BinaryOp::Lt | BinaryOp::Le => (left.as_ref(), right.as_ref()),
                BinaryOp::Gt | BinaryOp::Ge => (right.as_ref(), left.as_ref()),
                _ => return None,
            };
            let Expression::Function { name, args, .. } = dist else { return None };
            if !(name.eq_ignore_ascii_case("point.distance") || name.eq_ignore_ascii_case("distance")) || args.len() != 2 {
                return None;
            }
            let (prop, centre) = match (property_of_var(&args[0]), property_of_var(&args[1])) {
                (Some(p), None) => (p, &args[1]),
                (None, Some(p)) => (p, &args[0]),
                _ => return None,
            };
            let (cx, cy, geographic) = constant_point(centre)?;
            let r = constant_number(bound)?;
            // NaN or negative: no region to search.
            if r.is_nan() || r < 0.0 {
                return None;
            }
            let rect = if geographic {
                geographic_box(cx, cy, r)
            } else {
                Rect { min_x: cx - r, min_y: cy - r, max_x: cx + r, max_y: cy + r }
            };
            Some((prop, rect))
        }
        _ => None,
    }
}

/// Scan the nodes a point index places inside a region. A superset of the
/// answer: the predicate that produced the region filters above it.
pub struct PointIndexScanOperator {
    variable: String,
    label: Label,
    property: String,
    region: Rect,
    ids: Option<std::vec::IntoIter<NodeId>>,
}

impl PointIndexScanOperator {
    pub fn new(variable: String, label: Label, property: String, region: Rect) -> Self {
        Self { variable, label, property, region, ids: None }
    }
}

impl PhysicalOperator for PointIndexScanOperator {
    fn next(&mut self, store: &GraphStore) -> ExecutionResult<Option<Record>> {
        if self.ids.is_none() {
            let mut ids = store
                .point_index
                .with(self.label.as_str(), &self.property, |i| i.within(&self.region))
                .unwrap_or_default();
            // Ascending, like every other scan: downstream code that assumes a
            // scan's order (#1364) should not learn otherwise from this one.
            ids.sort();
            self.ids = Some(ids.into_iter());
        }
        for id in self.ids.as_mut().unwrap().by_ref() {
            if store.get_node(id).is_some_and(|n| n.labels.contains(&self.label)) {
                let mut r = Record::new();
                r.bind(self.variable.clone(), Value::NodeRef(id));
                return Ok(Some(r));
            }
        }
        Ok(None)
    }

    fn reset(&mut self) {
        self.ids = None;
    }

    fn estimate_rows(&self, store: &GraphStore, _children: &[Option<f64>]) -> Option<f64> {
        // The region's share of the index's own extent is not tracked, so the
        // estimate is the conventional range selectivity over the label.
        Some(store.label_node_count(&self.label) as f64 * 0.1)
    }

    fn describe(&self) -> OperatorDescription {
        let r = &self.region;
        OperatorDescription {
            name: "PointIndexScan".to_string(),
            details: format!(
                "var={}, {}.{} within [{}, {}]..[{}, {}]",
                self.variable,
                self.label.as_str(),
                self.property,
                r.min_x, r.min_y, r.max_x, r.max_y
            ),
            children: Vec::new(),
        }
    }
}

/// `CALL db.index.point.nearest(label, property, point, k) YIELD node, distance`.
///
/// Nearest by the index's planar order, then re-ranked by the distance
/// `point.distance` reports -- metres for a geographic point. For a geographic
/// index the planar candidates are widened to `4k` first, because degrees of
/// longitude shrink with latitude and the planar `k` nearest need not be the
/// great-circle `k` nearest.
pub struct PointNearestOperator {
    label: String,
    property: String,
    centre: PropertyValue,
    k: usize,
    node_var: String,
    distance_var: Option<String>,
    results: Option<std::vec::IntoIter<(NodeId, f64)>>,
}

impl PointNearestOperator {
    pub fn new(label: String, property: String, centre: PropertyValue, k: usize, node_var: String, distance_var: Option<String>) -> Self {
        Self { label, property, centre, k, node_var, distance_var, results: None }
    }
}

fn haversine(a: (f64, f64), b: (f64, f64)) -> f64 {
    const EARTH_RADIUS_M: f64 = 6_371_008.8;
    let (lon1, lat1, lon2, lat2) = (a.0.to_radians(), a.1.to_radians(), b.0.to_radians(), b.1.to_radians());
    let h = ((lat2 - lat1) / 2.0).sin().powi(2) + lat1.cos() * lat2.cos() * ((lon2 - lon1) / 2.0).sin().powi(2);
    2.0 * EARTH_RADIUS_M * h.sqrt().asin()
}

impl PhysicalOperator for PointNearestOperator {
    fn next(&mut self, store: &GraphStore) -> ExecutionResult<Option<Record>> {
        if self.results.is_none() {
            let (cx, cy, geographic) = point_coords(&self.centre).ok_or_else(|| {
                ExecutionError::TypeError("db.index.point.nearest: the third argument must be a point".to_string())
            })?;
            let fetch = if geographic { self.k.saturating_mul(4).max(self.k) } else { self.k };
            let found = store
                .point_index
                // Planar distance is what the tree orders by; a geographic
                // index reports metres, from the stored coordinates, below.
                .with(&self.label, &self.property, |i| i.nearest(cx, cy, fetch))
                .ok_or_else(|| {
                    ExecutionError::RuntimeError(format!(
                        "no point index on :{}({}). CREATE POINT INDEX FOR (n:{}) ON (n.{}) creates one.",
                        self.label, self.property, self.label, self.property
                    ))
                })?;
            let mut ranked: Vec<(NodeId, f64)> = if geographic {
                found
                    .into_iter()
                    .filter_map(|(id, _)| {
                        let v = store.node_properties_merged(id).get(&self.property).cloned()?;
                        let (x, y, _) = point_coords(&v)?;
                        Some((id, haversine((cx, cy), (x, y))))
                    })
                    .collect()
            } else {
                found
            };
            ranked.sort_by(|a, b| a.1.partial_cmp(&b.1).unwrap_or(std::cmp::Ordering::Equal));
            ranked.truncate(self.k);
            self.results = Some(ranked.into_iter());
        }
        Ok(self.results.as_mut().unwrap().next().map(|(id, d)| {
            let mut r = Record::new();
            r.bind(self.node_var.clone(), Value::NodeRef(id));
            if let Some(v) = &self.distance_var {
                r.bind(v.clone(), Value::Property(PropertyValue::Float(d)));
            }
            r
        }))
    }

    fn reset(&mut self) {
        self.results = None;
    }

    fn describe(&self) -> OperatorDescription {
        OperatorDescription {
            name: "PointNearest".to_string(),
            details: format!(":{}({}) k={}", self.label, self.property, self.k),
            children: Vec::new(),
        }
    }
}
