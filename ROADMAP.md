# Roadmap

This page says what is accepted and how something gets there. It does not list
the work itself: a list copied into a file is out of date the week after it is
written, which is how the previous `ROADMAP.md` became a development history and
was removed (#1835). The issue tracker is the list, and the links below are live.

## Accepted

Accepted work is an open issue labelled with a **horizon**.

| Horizon | Due | Issues |
|---|---|---|
| **H1** | 2026-12-31 | [open](https://github.com/samyama-ai/samyama-graph/issues?q=is%3Aissue+is%3Aopen+label%3AH1) · [done](https://github.com/samyama-ai/samyama-graph/issues?q=is%3Aissue+is%3Aclosed+label%3AH1+reason%3Acompleted) |

Most H1 issues are named after a requirement in the product specification
(`API-09`, `DX-11`, …) and say in their body what has to exist for the
requirement to be met. Issues without a horizon label are not yet committed to
any date. An issue being open is not, by itself, a commitment.

## How a request is accepted, or not

Open an issue. Within 2 business days a maintainer responds
([SUPPORT.md](SUPPORT.md)). The answer is one of:

- **accepted**: it gets a horizon label;
- **needs more information**: the issue stays open and says what is missing;
- **duplicate**: it is closed, pointing at the issue it duplicates;
- **not planned**: it is closed as *not planned*, with the reason in the issue.

## Not planned

There is no published list of non-goals yet. Until there is, the way to find
out whether something is out of scope is to ask in an issue, and the answer,
with its reason, will be on that issue.

From the publication of this page, a declined request is closed as
[not planned](https://github.com/samyama-ai/samyama-graph/issues?q=is%3Aissue+is%3Aclosed+reason%3A%22not+planned%22).
Before it, that reason was used for other things (work split into another
issue, a defect fixed some other way), so the older entries there are not
declined requests. Design alternatives considered and rejected are recorded in
the [architecture decision records](docs/ADR/).
