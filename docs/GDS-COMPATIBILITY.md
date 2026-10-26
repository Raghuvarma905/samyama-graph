# GDS procedure names

Neo4j GDS procedure names resolve to our algorithms where the two compute the
same answer. This document is the mapping and, more usefully, the list of names
that deliberately do **not** resolve.

Measured by `examples/gds_aliases.rs` (INT-04). Run it to reproduce the table:

```text
cargo run --release --example gds_aliases -- --json gds.json
```

## What resolves

A `gds.` name is canonicalised by dropping the `alpha.`/`beta.` maturity
segment, which carries no semantics, and a trailing `.stream`. Everything else
is left on the name, so it does not resolve.

| GDS | ours | note |
|---|---|---|
| `gds.pageRank.stream` | `pageRank` | |
| `gds.wcc.stream` | `wcc` | |
| `gds.scc.stream` | `scc` | |
| `gds.triangleCount.stream` | `triangleCount` | count per node, as in GDS |
| `gds.localClusteringCoefficient.stream` | `lcc` | renamed |
| `gds.labelPropagation.stream` | `cdlp` | renamed |
| `gds.louvain.stream` | `louvain` | |
| `gds.nodeSimilarity.stream` | `nodeSimilarity` | |
| `gds.degree.stream` | `degree` | |
| `gds.betweenness.stream` | `betweenness` | |
| `gds.closeness.stream` | `closeness` | |
| `gds.eigenvector.stream` | `eigenvector` | |
| `gds.kcore.stream` | `kcore` | |
| `gds.spanningTree.stream` | `mst` | renamed |
| `gds.maxFlow.stream` | `maxflow` | |
| `gds.shortestPath.dijkstra.stream` | `weightedPath` | **not** `shortestPath` |
| `gds.alpha.jaccard.stream` | `jaccard` | |
| `gds.alpha.adamicAdar.stream` | `adamicAdar` | |

`gds.shortestPath.dijkstra` is the one to read twice. GDS's dijkstra is
weighted; our `shortestPath` is an unweighted BFS and our `weightedPath` is the
weighted one. The obvious mapping is the wrong one, and it would have returned
plausible numbers while answering a different question.

## What does not resolve, and why

### Execution modes

Every algorithm here streams a row per node. GDS's `write` and `mutate` persist
a property and `stats` returns a summary. Those names are refused, with the
reason rather than "Unknown procedure":

```text
CALL gds.pageRank.write()
-> `gds.pageRank.write` is not supported: this engine streams results, and
   GDS's `write` mode returns a summary row instead. To store the result on
   each node, call `algo.pagerank({writeProperty: 'name'})`; it streams the
   rows as well.
```

The write itself exists: `algo.<name>({writeProperty: '...'})` stores each
node's result (see "Write-back" in `ALGORITHM-CONVENTIONS.md`). It is not
aliased onto `gds.<name>.write` because GDS's write mode returns one summary
row (`nodePropertiesWritten`, ...) where ours returns the rows, and running a
different result shape under a name the user already trusts is worse than not
recognising the name. `mutate` needs graph projections, which do not exist
yet.

### Divergent semantics

| GDS | why it is not aliased |
|---|---|
| `gds.alpha.triangles` | lists one row per triangle; our `triangleCount` returns a count per node — same word, different result shape |

### Not implemented

`gds.graph.project`, `gds.fastRP.stream`, `gds.knn.stream`,
`gds.beta.node2vec.stream`, `gds.alpha.ml.linkPrediction.train`. These have no
counterpart here. They are listed so the absence is a statement rather than a
gap in the table.

There is no named graph catalog: GDS projects a graph before running anything,
and our algorithms run against the store directly. A port drops the
`gds.graph.project` call rather than translating it.

## What is not measured

`examples/gds_aliases.rs` measures whether a **name resolves**. It does not run
GDS, so "the same answer" in the table above is read from our implementation and
GDS's documented definition — it is an assertion, not a comparison. Two entries
were wrong in the first draft of that list, so treat the equivalence column as
the weaker half of this document.

Comparing outputs against a GDS instance on the same graph is the measurement
that would settle it, and it has not been done.
