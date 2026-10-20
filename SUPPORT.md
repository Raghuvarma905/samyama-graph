# Getting help

## Where to go

| You have | Go to |
|---|---|
| A bug, or a feature request | [Open an issue](https://github.com/samyama-ai/samyama-graph/issues/new/choose) |
| A question about using Samyama | The [community chat](https://chat.whatsapp.com/Jjjkb3uWRDi1YMdfffaD9d), or the [book](https://graph.samyama.cloud/book/) first |
| A security vulnerability | **Not an issue.** Follow [SECURITY.md](SECURITY.md), which has its own response times |
| A question about what is planned | [ROADMAP.md](ROADMAP.md) |

## What to expect when you open an issue

**A maintainer responds to every issue within 2 business days.**

- **A response** is a comment from a maintainer, or a maintainer closing the issue
  with a reason. Adding a label or an assignee does not count: GitHub does not
  tell you when that happens, so from where you sit nothing has happened.
- **Business days** are Monday to Friday, in India Standard Time (Asia/Kolkata),
  where the maintainers are. Public holidays are not excluded.
- **A maintainer** is anyone with triage access to this repository or higher.
- It is a promise about a **first response**, not about a fix or a release date.
  The first response says what happens next: we need more information, it is
  accepted (and labelled with a horizon, see [ROADMAP.md](ROADMAP.md)), it is a
  duplicate, or it is not something we will do, and why.

It covers issues in this repository. It does not cover pull requests, the
community chat, or security reports, which [SECURITY.md](SECURITY.md) handles
under a separate promise: acknowledgement within 3 business days, triage within 7.

## How it is measured

[`scripts/triage_sla.py`](scripts/triage_sla.py) reads the issue tracker and
reports the first-response time of every issue opened by someone outside the
maintainer team. The [Triage SLA workflow](.github/workflows/triage-sla.yml) runs
it every weekday and fails when an issue opened since this page was published
missed the 2 days. Its job summary is the current record. You can run it
yourself:

    GITHUB_TOKEN=$(gh auth token) python3 scripts/triage_sla.py

## The record before this page

Until this page was published there was no promise, and the two issues opened
from outside the team both waited far longer than 2 business days:

| Issue | Opened | First response | Business days |
|---|---|---|---:|
| [#275](https://github.com/samyama-ai/samyama-graph/issues/275) | 2026-07-02 | a comment, 2026-09-30 | 64 |
| [#542](https://github.com/samyama-ai/samyama-graph/issues/542) | 2026-08-17 | closed, 2026-09-30, with no comment | 33 |

They are listed here because a promise is only worth what its record shows, and
the record starts on the day this page was published.
