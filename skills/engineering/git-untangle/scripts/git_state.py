#!/usr/bin/env python3
"""
git_state.py -- say what is actually going on in this repository, in plain language.

READ-ONLY. Runs nothing that changes the repository: no fetch, no checkout, no stash,
no config edits. Safe to run any time, including when you have no idea what state you
are in -- which is the point.

Most git confusion is not a hard problem, it is an unreadable status. `git status` will
tell you a submodule is "modified" when nobody edited anything, will not tell you your
upstream branch was deleted, and will not tell you which of three remotes a push would
go to. This prints the things that actually decide what you should do next, and says
what each one means.

Exit code is 0 unless something needs a decision from you (2), so it can gate a script.

Usage:
    git_state.py [PATH]      # default: current directory
    git_state.py --brief     # one line per finding, no explanations
"""
import argparse
import os
import subprocess
import sys


def git(args, cwd):
    try:
        r = subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True, timeout=30)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except Exception as exc:                                        # noqa: BLE001
        return 1, "", str(exc)


class Report:
    def __init__(self, brief):
        self.brief = brief
        self.needs_decision = False

    def ok(self, line):
        print(f"  ok    {line}")

    def note(self, line, why=None):
        print(f"  note  {line}")
        if why and not self.brief:
            print(f"        {why}")

    def act(self, line, why=None, fix=None):
        self.needs_decision = True
        print(f"  ACT   {line}")
        if why and not self.brief:
            print(f"        {why}")
        if fix and not self.brief:
            print(f"        -> {fix}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", nargs="?", default=".")
    ap.add_argument("--brief", action="store_true", help="findings only, no explanations")
    a = ap.parse_args()
    cwd = os.path.abspath(a.path)

    rc, top, _ = git(["rev-parse", "--show-toplevel"], cwd)
    if rc != 0:
        print(f"not a git repository: {cwd}")
        return 1
    r = Report(a.brief)
    print(f"\n{top}\n")

    # ---- where am I -------------------------------------------------------
    _, branch, _ = git(["rev-parse", "--abbrev-ref", "HEAD"], cwd)
    _, sha, _ = git(["rev-parse", "--short", "HEAD"], cwd)
    if branch == "HEAD":
        r.act(f"detached HEAD at {sha}",
              "You are not on a branch. Commits made here are not on any branch and are "
              "easy to lose track of.",
              "git switch -c some-name   (keep them)   or   git switch main   (discard)")
    else:
        r.ok(f"on branch '{branch}' at {sha}")

    # ---- in the middle of something ---------------------------------------
    gitdir = os.path.join(top, ".git")
    for marker, what, fix in (
        ("MERGE_HEAD", "a merge is in progress", "git merge --continue   or   git merge --abort"),
        ("rebase-merge", "a rebase is in progress", "git rebase --continue   or   git rebase --abort"),
        ("rebase-apply", "a rebase or am is in progress", "git rebase --abort   or   git am --abort"),
        ("CHERRY_PICK_HEAD", "a cherry-pick is in progress", "git cherry-pick --continue / --abort"),
    ):
        if os.path.exists(os.path.join(gitdir, marker)):
            r.act(what, "Finish or abort it before doing anything else; other commands "
                        "will behave strangely until you do.", fix)

    # ---- real edits vs submodule drift ------------------------------------
    # The distinction that causes the most needless alarm: a submodule sitting at a
    # commit other than the one recorded shows up as a "modified" entry, so a tree
    # nobody has touched can report several modified "files".
    _, real, _ = git(["status", "--porcelain", "--ignore-submodules=all"], cwd)
    _, allst, _ = git(["status", "--porcelain"], cwd)
    real_n = len([l for l in real.splitlines() if l.strip()])
    all_n = len([l for l in allst.splitlines() if l.strip()])
    _, subs, _ = git(["submodule", "status", "--recursive"], cwd)
    drift = [l for l in subs.splitlines() if l.startswith("+")]
    missing = [l for l in subs.splitlines() if l.startswith("-")]

    if real_n:
        r.note(f"{real_n} file(s) with real edits",
               "Actual changes you (or a tool) made. Commit, stash or discard them.")
    else:
        r.ok("no edited files")

    if drift:
        names = ", ".join(l.split()[1] for l in drift[:4])
        r.act(f"{len(drift)} submodule(s) not at the recorded commit: {names}"
              + (" ..." if len(drift) > 4 else ""),
              "This is NOT uncommitted work -- nobody edited these. The submodule is "
              "checked out at a different commit than this branch records. Scripts that "
              "count `git status` lines treat it as dirt and refuse to act.",
              "git submodule update --init --recursive")
    if missing:
        r.act(f"{len(missing)} submodule(s) not initialised",
              "The directory exists but is empty. Anything building from it is building "
              "against nothing, usually without saying so.",
              "git submodule update --init --recursive")
    if not drift and not missing and all_n != real_n:
        r.note("submodule entries differ but are not drifted", "usually untracked content inside one")

    # ---- upstream ---------------------------------------------------------
    rc_up, upstream, _ = git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], cwd)
    if rc_up != 0:
        r.note("this branch has no upstream",
               "A bare `git push` will not know where to go.")
    else:
        remote = upstream.split("/")[0]
        rc_e, _, _ = git(["rev-parse", "--verify", "--quiet", f"refs/remotes/{upstream}"], cwd)
        if rc_e != 0:
            r.act(f"upstream '{upstream}' no longer exists",
                  "The branch you track was deleted or renamed on the remote. Pushing may "
                  "RECREATE the deleted branch and re-split history.",
                  "git fetch --prune, then set the new upstream: "
                  "git branch -u <remote>/<branch>")
        else:
            _, counts, _ = git(["rev-list", "--left-right", "--count", f"{upstream}...HEAD"], cwd)
            try:
                behind, ahead = (int(x) for x in counts.split())
            except ValueError:
                behind = ahead = 0
            if ahead and behind:
                r.act(f"{ahead} ahead, {behind} behind {upstream} -- diverged",
                      "Both sides moved. A merge or rebase is needed; a plain push will be "
                      "rejected.", "git pull --rebase   (then re-check before pushing)")
            elif ahead:
                r.note(f"{ahead} commit(s) to push to {upstream}", f"git push {remote} {branch}")
            elif behind:
                r.note(f"{behind} commit(s) to pull from {upstream}", "git pull --ff-only")
            else:
                r.ok(f"in sync with {upstream}")

    # ---- which remote would this go to ------------------------------------
    _, remotes, _ = git(["remote", "-v"], cwd)
    push = {}
    for line in remotes.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[2] == "(push)":
            push[parts[0]] = parts[1]
    if len(push) > 1:
        r.act(f"{len(push)} push remotes: " + ", ".join(f"{k} -> {v}" for k, v in push.items()),
              "More than one destination. Work meant for one can land on the other, and "
              "that is not always reversible -- a public remote especially.",
              "name the remote explicitly: git push <remote> <branch>")
    elif push:
        (k, v), = push.items()
        r.ok(f"pushes to {k} -> {v}")

    # ---- things people forget ---------------------------------------------
    _, stash, _ = git(["stash", "list"], cwd)
    n_stash = len([l for l in stash.splitlines() if l.strip()])
    if n_stash:
        r.note(f"{n_stash} stash entr{'y' if n_stash == 1 else 'ies'}",
               "Easy to forget and easy to lose. `git stash list` to see them.")

    _, untracked, _ = git(["ls-files", "--others", "--exclude-standard", "--directory"], cwd)
    n_unt = len([l for l in untracked.splitlines() if l.strip()])
    if n_unt:
        r.note(f"{n_unt} untracked path(s)",
               "Not ignored and not added -- they will not be in any commit.")

    print()
    if r.needs_decision:
        print("  Something above needs a decision. Nothing was changed by this command.\n")
        return 2
    print("  Nothing needs a decision.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
