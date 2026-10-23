#!/usr/bin/env python3
"""Exercise SLA role classification with the workflow's own GitHub token.

The fixtures must name a current repository maintainer and a public user without
triage access. This performs two read-only permission lookups, not a tracker scan.
"""
import argparse
import os

from triage_sla import GitHub


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', required=True)
    parser.add_argument('--maintainer', required=True)
    parser.add_argument('--outside-user', required=True)
    args = parser.parse_args()
    token = os.environ.get('GITHUB_TOKEN')
    if not token:
        parser.error('GITHUB_TOKEN is required; this check must exercise authenticated access')
    if args.maintainer.casefold() == args.outside_user.casefold():
        parser.error('role fixtures must be different users')
    gh = GitHub(args.repo, token)
    if not gh.is_maintainer(args.maintainer):
        raise SystemExit(f'expected {args.maintainer} to resolve as a maintainer')
    if gh.is_maintainer(args.outside_user):
        raise SystemExit(f'expected {args.outside_user} to resolve without triage access')
    print('PASS: workflow token distinguishes the maintainer and outside-user fixtures')


if __name__ == '__main__':
    main()
