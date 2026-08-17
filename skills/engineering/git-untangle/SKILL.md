---
name: git-untangle
description: Explain what state a git repository is actually in, in plain language, and what to do about it. Use when git is confusing - a push was rejected, a branch or upstream vanished, "modified" files nobody edited, a detached HEAD, diverged branches, submodules that look dirty, or you simply want to know whether it is safe to push. Read-only diagnosis first, then the specific recovery.
---

# git-untangle

Most git confusion is not a hard problem. It is an unreadable status.

`git status` will tell you a submodule is "modified" when nobody edited anything, will
not mention that the branch you track was deleted on the remote, and will not tell you
which of three remotes a push would go to. So people either guess, or run something
destructive they found online. This skill reads the state, says what it means, and
gives the one recovery that fits.

## Start here, always

```sh
python3 scripts/git_state.py [PATH]        # read-only; changes nothing
python3 scripts/git_state.py --brief       # findings only
```

It reports the things that actually decide what to do next: branch and whether you are
on one, operations left half-finished, real edits **separated from** submodule drift,
upstream existence and ahead/behind, how many push destinations exist, stashes and
untracked paths. Exit code is `2` if something needs a decision, `0` if not.

**Run it before asking git to do anything you are unsure about.** It cannot make things
worse — it never fetches, checks out, stashes, resets or edits config.

## The states that cause the most trouble

### "Modified" files nobody edited

Almost always **submodule drift**: the submodule is checked out at a commit other than
the one your branch records. It is not uncommitted work and there is nothing to resolve
by hand.

It matters more than it looks, because tooling counts `git status` lines to decide
whether a tree is safe to touch. A setup script that sees "3 modified files" will refuse
to update — so the checkout that most needs updating is the one that gets skipped, and
the message blames you for edits you never made.

```sh
git submodule update --init --recursive
```

To tell the two apart yourself:

```sh
git status --porcelain --ignore-submodules=all   # real edits only
git submodule status --recursive | grep '^+'     # drifted
git submodule status --recursive | grep '^-'     # never initialised (empty dir!)
```

A `-` is worse than a `+`: the directory exists and is empty, so anything building from
it is building against nothing, usually without complaining.

### The branch I track has disappeared

Someone renamed or deleted it on the remote. **Do not push to recreate it** — that
resurrects a deleted branch and re-splits history, which is a much bigger mess than the
one you have.

```sh
git fetch --prune                      # drop the stale remote-tracking ref
git branch -m old new                  # if your local name should change too
git branch -u origin/new new           # point at the branch that exists now
```

### A push was rejected

You are behind, or you have diverged. `git_state.py` says which.

```sh
git pull --ff-only      # behind only: safe, refuses if it would need a merge
git pull --rebase       # diverged: replays your commits on top
```

**Never reach for `--force`.** If you think you need it, that is the moment to stop and
ask someone. Force-pushing discards work that someone else may already have pulled, and
the person who loses it usually is not you.

### Detached HEAD

You are not on a branch, so commits you make belong to nothing and are easy to lose.

```sh
git switch -c keep-this     # keep the work on a new branch
git switch main             # abandon it and go back
```

### More than one remote

Two push destinations means work meant for one can land on the other. If one of them is
public, that is not reversible in any way that matters — a secret pushed to a public
remote is a leaked secret even after you delete it, because it was fetchable and may be
mirrored.

```sh
git remote -v                   # what exists
git push <remote> <branch>      # always name both when more than one exists
```

Before a first push to a public remote, look at what is actually in the commit —
`git show --stat HEAD` and read the file list. Example paths, comments and test
fixtures leak more than people expect.

## Rules worth keeping

- **Diagnose before acting.** `git_state.py` is free and read-only.
- **Never force-push** to anything shared.
- **Prefer `git pull --ff-only`.** It refuses rather than silently creating a merge.
- **A deleted upstream is not an invitation to recreate it.** Fetch, prune, re-point.
- **Submodule drift is not dirt.** Update it; do not "resolve" it.
- **Name the remote and branch on push** whenever more than one remote exists.
- **`git status` lines are not a safety check.** Anything scripted that counts them
  should use `--ignore-submodules=all`, or it will trip over drift.

## When someone else is working in the same tree

Concurrent sessions, agents or CI can move refs under you between two of your commands.
If something that worked a minute ago now fails:

```sh
git fetch --prune && python3 scripts/git_state.py
```

That is usually the whole fix: your view was stale, not your repository broken. Re-check
before pushing, because the tip may have moved and your push would otherwise be rejected
or, worse, land on a branch that has since been repurposed.
