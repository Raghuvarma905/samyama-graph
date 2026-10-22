#!/usr/bin/env python3
"""Measure the first-response SLA that SUPPORT.md promises (DX-11, #1785).

The promise: an issue opened by someone outside the maintainer team gets a first
response within 2 business days. A promise nobody measures is a sentence, so
this reads the issue tracker and reports, for every such issue, how long the
first response took -- and exits non-zero if any issue opened since the policy
took effect missed it.

Who is a maintainer. Anyone whose role on this repository is triage or higher,
asked per person through the collaborator-permission endpoint. Not the
`author_association` field on issues: this organisation has no public members,
and a workflow's GITHUB_TOKEN cannot see private membership, so in CI every
maintainer would come back `CONTRIBUTOR` and every team issue would look like
an outside report. If the lookup finds no maintainer at all among the authors,
it is the lookup that is broken, and the script stops rather than report.

What counts as a response. A non-empty comment by a maintainer other than the
reporter, including the explanation accompanying a closure. A silent closure,
label, an
assignment or a milestone does not notify anyone, so it is not a response to
the person waiting, even though it is triage. Bot comments do not count.

Business days are Monday to Friday in Asia/Kolkata, where the maintainers are.
Public holidays are not excluded, which makes the measured promise stricter
than the written one, never looser.

When the policy took effect. The first-parent integration commit that added
SUPPORT.md to the default branch, rather than its earlier development-branch
commit. Issues opened before then are reported for the record but
cannot fail the check: the policy was not a promise yet.

Run:  GITHUB_TOKEN=$(gh auth token) python3 scripts/triage_sla.py
Test: python3 scripts/test_triage_sla.py
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import sys
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

REPO = "samyama-ai/samyama-graph"
POLICY_FILE = "SUPPORT.md"
SLA_BUSINESS_DAYS = 2
TZ = ZoneInfo("Asia/Kolkata")
MAINTAINER_ROLES = {"admin", "maintain", "write", "triage"}


# ---------------------------------------------------------------- the rules --

def business_seconds(start: datetime, end: datetime) -> float:
    """Seconds between two instants that fall on a Monday-to-Friday in TZ."""
    if end <= start:
        return 0.0
    total = 0.0
    cur = start.astimezone(TZ)
    end = end.astimezone(TZ)
    while cur < end:
        next_midnight = (cur + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        stop = min(next_midnight, end)
        if cur.weekday() < 5:
            total += (stop - cur).total_seconds()
        cur = stop
    return total


def business_days(start: datetime, end: datetime) -> float:
    return business_seconds(start, end) / 86400.0


@dataclass
class Issue:
    number: int
    title: str
    author: str
    created_at: datetime
    url: str


@dataclass
class Event:
    """A comment or a close, already reduced to who did it and when."""
    kind: str          # "comment" | "closed"
    actor: str
    actor_is_bot: bool
    at: datetime
    body: str = ""


def first_response(issue: Issue, events: list[Event], is_maintainer) -> Event | None:
    """The earliest event that counts as a response under the policy, if any."""
    for e in sorted(events, key=lambda e: e.at):
        if e.kind != "comment" or not e.body.strip() or e.actor_is_bot or e.actor == issue.author:
            continue
        if is_maintainer(e.actor):
            return e
    return None


@dataclass
class Verdict:
    issue: Issue
    response: Event | None
    elapsed_days: float      # to the response, or to `now` if there is none
    in_scope: bool           # opened on or after the policy took effect

    @property
    def met(self) -> bool:
        return self.elapsed_days <= SLA_BUSINESS_DAYS

    @property
    def status(self) -> str:
        if self.response is None:
            return "waiting (within SLA)" if self.met else "no response (missed)"
        return "met" if self.met else "missed"


def judge(issue: Issue, events: list[Event], is_maintainer, effective: datetime | None,
          now: datetime) -> Verdict:
    r = first_response(issue, events, is_maintainer)
    elapsed = business_days(issue.created_at, r.at if r else now)
    in_scope = effective is not None and issue.created_at >= effective
    return Verdict(issue, r, elapsed, in_scope)


# --------------------------------------------------------------- GitHub I/O --

def _ts(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def policy_publication(repo_root: Path, ref: str) -> datetime | None:
    """Find the policy's entry onto the default branch's first-parent history."""
    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo_root), *args],
                                       text=True, stderr=subprocess.PIPE, timeout=60).strip()

    if git("rev-parse", "--is-shallow-repository") == "true":
        raise RuntimeError("policy publication needs full Git history; fetch with depth 0")
    git("rev-parse", "--verify", f"{ref}^{{commit}}")
    present = subprocess.run(["git", "-C", str(repo_root), "cat-file", "-e",
                              f"{ref}:{POLICY_FILE}"], capture_output=True, timeout=60)
    if present.returncode:
        return None
    dates = git("log", "--first-parent", "--diff-merges=first-parent", "--diff-filter=A",
                "--reverse", "--format=%cI", ref, "--", POLICY_FILE).splitlines()
    if not dates:
        raise RuntimeError("could not locate the policy's introduction on the default branch")
    return datetime.fromisoformat(dates[0]).astimezone(timezone.utc)


class GitHub:
    def __init__(self, repo: str, token: str | None):
        self.repo = repo
        self.token = token
        self._roles: dict[str, str] = {}

    def _get(self, url: str):
        if not url.startswith("https://"):
            url = f"https://api.github.com/repos/{self.repo}/{url}"
        req = urllib.request.Request(url, headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            **({"Authorization": f"Bearer {self.token}"} if self.token else {}),
        })
        # A page of issues is ~600 KB of bodies, and a truncated read
        # (IncompleteRead) or a 5xx is a network fact, not a missed SLA. Retry
        # those; a 4xx is an answer and is raised.
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    link = resp.headers.get("Link", "")
                    nxt = re.search(r'<([^>]+)>;\s*rel="next"', link)
                    return json.load(resp), (nxt.group(1) if nxt else None)
            except urllib.error.HTTPError as e:
                if e.code < 500 or attempt == 3:
                    raise
            except (http.client.IncompleteRead, urllib.error.URLError, TimeoutError):
                if attempt == 3:
                    raise
            time.sleep(2 ** attempt)

    def pages(self, path: str):
        url = path
        while url:
            data, url = self._get(url)
            yield from data

    def issues(self) -> list[Issue]:
        out = []
        for it in self.pages("issues?state=all&per_page=100&sort=created&direction=asc"):
            if "pull_request" in it:
                continue
            out.append(Issue(it["number"], it["title"], it["user"]["login"],
                             _ts(it["created_at"]), it["html_url"]))
        return out

    def events(self, number: int) -> list[Event]:
        out = []
        for c in self.pages(f"issues/{number}/comments?per_page=100"):
            out.append(Event("comment", c["user"]["login"], c["user"]["type"] == "Bot",
                             _ts(c["created_at"]), c.get("body") or ""))
        return out

    def is_maintainer(self, login: str) -> bool:
        if login not in self._roles:
            try:
                data, _ = self._get(f"collaborators/{login}/permission")
            except urllib.error.HTTPError as e:
                if e.code != 404:   # a deleted account ("ghost") has no role; anything else is real
                    raise
                data = {}
            self._roles[login] = data.get("role_name") or data.get("permission") or "none"
        return self._roles[login] in MAINTAINER_ROLES

    def policy_effective(self) -> datetime | None:
        info, _ = self._get(f"https://api.github.com/repos/{self.repo}")
        branch = info["default_branch"]
        repo_root = Path(__file__).resolve().parents[1]
        ref = "refs/triage-sla/default"
        subprocess.run(["git", "-C", str(repo_root), "fetch", "--quiet", "--no-tags",
                        f"https://github.com/{self.repo}.git", f"+refs/heads/{branch}:{ref}"],
                       check=True, capture_output=True, timeout=120)
        return policy_publication(repo_root, ref)


# ------------------------------------------------------------------ report --

def render(verdicts: list[Verdict], effective: datetime | None) -> str:
    lines = ["## First-response SLA (SUPPORT.md, DX-11)", ""]
    if effective is None:
        lines.append(f"Not in effect: `{POLICY_FILE}` is not on the default branch yet. "
                     "Nothing below can fail the check.")
    else:
        lines.append(f"In effect since {effective:%Y-%m-%d %H:%M} UTC.")
    lines += ["", (f"Target: a first response within {SLA_BUSINESS_DAYS} business days "
                   "(Mon-Fri, Asia/Kolkata) on issues opened by people without triage access."), ""]
    if not verdicts:
        lines.append("No issues from outside the maintainer team.")
        return "\n".join(lines)
    lines += ["| Issue | Opened | First response | Business days | Status | Counts |",
              "|---|---|---|---:|---|---|"]
    for v in verdicts:
        resp = f"{v.response.kind} by @{v.response.actor}, {v.response.at:%Y-%m-%d}" if v.response else "--"
        lines.append(f"| [#{v.issue.number}]({v.issue.url}) {v.issue.title[:60]} "
                     f"| {v.issue.created_at:%Y-%m-%d} | {resp} | {v.elapsed_days:.1f} "
                     f"| {v.status} | {'yes' if v.in_scope else 'before policy'} |")
    scoped = [v for v in verdicts if v.in_scope]
    if scoped:
        met = sum(v.met for v in scoped)
        lines += ["", f"Since the policy took effect: {met} of {len(scoped)} met."]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--repo", default=REPO)
    ap.add_argument("--since", help="override the effective date (YYYY-MM-DD, UTC); for trying it out")
    args = ap.parse_args(argv)

    gh = GitHub(args.repo, os.environ.get("GITHUB_TOKEN"))
    now = datetime.now(timezone.utc)
    try:
        effective = (datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                     if args.since else gh.policy_effective())
        issues = gh.issues()
        authors = sorted({i.author for i in issues})
        team = {a for a in authors if gh.is_maintainer(a)}
        if issues and not team:
            print("error: no issue author resolved as a maintainer -- the permission lookup is "
                  "not seeing roles, so every issue would look external. Refusing to report.",
                  file=sys.stderr)
            return 2
        external = [i for i in issues if i.author not in team]
        verdicts = [judge(i, gh.events(i.number), gh.is_maintainer, effective, now) for i in external]
    except urllib.error.HTTPError as e:
        print(f"error: GitHub API {e.code} on {e.url}: {e.read()[:200]!r}", file=sys.stderr)
        return 2
    except (http.client.IncompleteRead, urllib.error.URLError, TimeoutError) as e:
        # Exit 2, not 1: "could not measure" must not read as "missed".
        print(f"error: could not read the issue tracker: {e!r}", file=sys.stderr)
        return 2
    except (OSError, RuntimeError, subprocess.SubprocessError) as e:
        print(f"error: could not determine policy publication: {e}", file=sys.stderr)
        return 2

    report = render(verdicts, effective)
    print(report)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write(report + "\n")
    missed = [v for v in verdicts if v.in_scope and not v.met]
    return 1 if missed else 0


if __name__ == "__main__":
    sys.exit(main())
