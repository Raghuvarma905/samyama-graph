#!/usr/bin/env python3
"""The SLA measurement must be wrong in neither direction.

A check that counts a label as a response passes an issue whose reporter heard
nothing; one that counts the weekend fails a Friday issue answered on Monday.
Both have the same shape -- a plausible number -- so each rule is pinned here,
offline, against hand-built issues rather than the live tracker.

Run: python3 scripts/test_triage_sla.py
"""

from __future__ import annotations

import sys
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from triage_sla import TZ, Event, Issue, business_days, judge, policy_publication

# release-bot has write access, as an app installed on the repo can.
TEAM = {"maint", "release-bot"}
is_maintainer = TEAM.__contains__


def ist(y, mo, d, h=0, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=TZ)


def issue(at, author="outsider"):
    return Issue(1, "t", author, at.astimezone(timezone.utc), "u")


def ev(kind, actor, at, bot=False, body="Thanks; we are investigating."):
    return Event(kind, actor, bot, at.astimezone(timezone.utc), body)


FAILURES = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        FAILURES.append(name)


# 2026-10-16 is a Friday, 2026-10-19 the Monday after.
FRI_15 = ist(2026, 10, 16, 15)
NOW = ist(2026, 12, 1)

check("a weekend does not count",
      abs(business_days(FRI_15, ist(2026, 10, 19, 15)) - 1.0) < 1e-9)
check("an issue opened on Saturday starts its clock on Monday",
      business_days(ist(2026, 10, 17, 10), ist(2026, 10, 19, 0)) == 0.0)
check("business days are counted in Asia/Kolkata, not UTC",
      # Sunday 20:00 UTC is Monday 01:30 IST: 1.5 business hours have passed.
      abs(business_days(ist(2026, 10, 18, 23, 59), datetime(2026, 10, 18, 20, tzinfo=timezone.utc))
          - 1.5 / 24) < 1e-9)

v = judge(issue(FRI_15), [ev("comment", "maint", ist(2026, 10, 20, 15))], is_maintainer, FRI_15, NOW)
check("Friday 15:00 answered Tuesday 15:00 is exactly 2 business days, and met",
      abs(v.elapsed_days - 2.0) < 1e-9 and v.met)

v = judge(issue(FRI_15), [ev("comment", "maint", ist(2026, 10, 20, 15, 1))], is_maintainer, FRI_15, NOW)
check("one minute past 2 business days is missed", not v.met and v.status == "missed")

v = judge(issue(FRI_15), [ev("comment", "outsider", ist(2026, 10, 16, 16)),
                          ev("comment", "someone", ist(2026, 10, 16, 17)),
                          ev("comment", "release-bot", ist(2026, 10, 16, 18), bot=True)],
          is_maintainer, FRI_15, NOW)
check("the author, a non-maintainer and a bot -- even one with write access -- are not a response",
      v.response is None and v.status == "no response (missed)")

v = judge(issue(FRI_15), [ev("closed", "maint", ist(2026, 10, 19, 9))], is_maintainer, FRI_15, NOW)
check("a maintainer closing without an explanation is not a response", v.response is None)

v = judge(issue(FRI_15), [ev("closed", "maint", ist(2026, 10, 19, 10)),
                         ev("comment", "maint", ist(2026, 10, 19, 9))], is_maintainer, FRI_15, NOW)
check("an explained closure counts its explanatory comment", v.response is not None and v.response.kind == "comment" and v.met)

v = judge(issue(FRI_15), [ev("comment", "maint", ist(2026, 10, 19, 9), body="  ")],
          is_maintainer, FRI_15, NOW)
check("an empty comment is not an explanation", v.response is None)

v = judge(issue(FRI_15), [ev("closed", "outsider", ist(2026, 10, 16, 16))], is_maintainer, FRI_15, NOW)
check("the author closing their own issue is not a response", v.response is None)

v = judge(issue(FRI_15), [], is_maintainer, FRI_15, ist(2026, 10, 19, 12))
check("an unanswered issue inside its window is waiting, not missed",
      v.met and v.status == "waiting (within SLA)")

v = judge(issue(FRI_15), [], is_maintainer, ist(2026, 10, 17), NOW)
check("an issue opened before the policy took effect is not in scope", not v.in_scope)

v = judge(issue(FRI_15), [], is_maintainer, None, NOW)
check("with no policy on the default branch, nothing is in scope", not v.in_scope)

v = judge(issue(FRI_15), [ev("comment", "maint", ist(2026, 10, 30)),
                          ev("comment", "maint", ist(2026, 10, 16, 16))], is_maintainer, FRI_15, NOW)
check("the earliest response is the one measured, whatever order the API returns",
      v.response.at == ist(2026, 10, 16, 16).astimezone(timezone.utc))

with tempfile.TemporaryDirectory(prefix="triage-policy-history-") as tmp:
    repo = Path(tmp) / "repo"
    repo.mkdir()
    def git(*args, at="2026-10-01T00:00:00+00:00"):
        return subprocess.check_output(
            ["git", "-C", str(repo), "-c", "user.name=Policy test",
             "-c", "user.email=policy@example.invalid", "-c", "commit.gpgsign=false",
             "-c", "core.hooksPath=/dev/null", *args],
            env={**os.environ, "GIT_AUTHOR_DATE": at, "GIT_COMMITTER_DATE": at},
            text=True, stderr=subprocess.PIPE).strip()
    git("init", "-b", "main")
    git("commit", "--allow-empty", "-m", "before policy")
    before = git("rev-parse", "HEAD")
    git("checkout", "-b", "dev")
    (repo / "SUPPORT.md").write_text("First-response policy\n")
    git("add", ".")
    git("commit", "-m", "draft policy", at="2026-10-02T00:00:00+00:00")
    git("checkout", "main")
    git("merge", "--no-ff", "dev", "-m", "publish policy", at="2026-10-22T00:00:00+00:00")
    effective = policy_publication(repo, "main")
    check("a delayed merge starts the policy on publication, not its source commit date",
          effective == datetime(2026, 10, 22, tzinfo=timezone.utc))
    check("an issue opened between drafting and publication is out of scope",
          not judge(issue(ist(2026, 10, 15)), [], is_maintainer, effective, NOW).in_scope)
    check("a branch without the policy has no effective date", policy_publication(repo, before) is None)
    shallow = Path(tmp) / "shallow"
    subprocess.run(["git", "clone", "--quiet", "--depth=1", repo.as_uri(), str(shallow)], check=True)
    try:
        policy_publication(shallow, "HEAD")
    except RuntimeError:
        check("a shallow history cannot silently invent an effective date", True)
    else:
        check("a shallow history cannot silently invent an effective date", False)

if FAILURES:
    print(f"\n{len(FAILURES)} failed")
    sys.exit(1)
print("\nall passed")
