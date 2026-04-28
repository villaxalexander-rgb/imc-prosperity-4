---
name: IMC Prosperity 4 project context
description: Repo is the user's IMC Prosperity 4 competition workspace, organized by round; Round 4 is current focus
type: project
originSessionId: 2170a43c-31aa-45ce-ad86-11c62fabcfe5
---
The repo at `/Users/alexandervillax/imc-prosperity-4` is the user's IMC
Prosperity 4 competition workspace. Each round lives in its own top-level
folder (`round_3/`, `round_4/`, …), and each round folder typically contains
a `trader/` (the script uploaded to IMC), `research/` (scripts, data, cached
results, experiment writeups), and `backtest_logs/` (per-submission logs).

**Why:** the user iterates on strategies submission-by-submission and keeps
the artifacts for each round side-by-side so older alphas can be referenced
when later rounds are evaluated.

**How to apply:** when the user says "save / drop into the repo", default to
placing artifacts under `round_<N>/` matching the bundle layout described in
that round's README. Don't fabricate missing pieces — if a bundle references
files that aren't on disk, save what's present and call out the gap.

Active source data and helpers commonly live outside the repo, especially:
- `~/Downloads/ROUND_<N>.zip` — round data dumps (prices/trades CSVs)
- `~/Desktop/Booth /IMC Prosperity/` — staging area for trader scripts
  (note the trailing space in "Booth ")
