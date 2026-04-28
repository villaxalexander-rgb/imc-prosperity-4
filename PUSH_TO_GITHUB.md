# How to push this repo to your GitHub

The repository is fully prepared in this folder. Run these commands in a
terminal **on your machine** (where you have your GitHub credentials).

## 1. Create the empty repo on GitHub

Go to <https://github.com/new>, set:

- **Repository name:** `imc-prosperity-4`
- **Visibility:** Public (or Private — your call)
- Leave "Initialize this repository" boxes **unchecked** — we'll push our own
  files.

## 2. Push from this folder

```bash
cd "/Users/alexandervillax/Library/Application Support/Claude/local-agent-mode-sessions/fe10b6e7-fbf7-4216-bf42-1aee69e7496b/90e4c5c0-4e92-46ed-886b-16a06de7a326/local_0f35eaaf-8740-40a0-b9b3-4ea9d8cf5879/outputs/imc-prosperity-4"

git init
git add .
git commit -m "Initial commit: round 3 trader + backtests + submission history"
git branch -M main

# Replace YOUR_USERNAME with your GitHub username
git remote add origin https://github.com/YOUR_USERNAME/imc-prosperity-4.git

git push -u origin main
```

If you'd rather move it out of the working folder first:

```bash
cp -r "/Users/alexandervillax/Library/Application Support/Claude/local-agent-mode-sessions/fe10b6e7-fbf7-4216-bf42-1aee69e7496b/90e4c5c0-4e92-46ed-886b-16a06de7a326/local_0f35eaaf-8740-40a0-b9b3-4ea9d8cf5879/outputs/imc-prosperity-4" ~/imc-prosperity-4
cd ~/imc-prosperity-4
# then run the git commands above
```

## 3. Future rounds

When Round 4 starts, just create `round_4/` next to `round_3/` and follow the
same layout. Update the top-level `README.md` to add the new round.

## What's included

- `README.md` — top-level overview
- `LICENSE` — MIT
- `.gitignore` — Python + OS junk
- `round_3/`
  - `trader.py` — the latest submitted algorithm (v12)
  - `backtest.py`, `backtest_per_day.py`, `backtest_judge_day2.py`
  - `REPORT.md` — strategy write-up
  - `SUBMISSION_HISTORY.md` — full chronology of every submission with
    diagnoses and learnings
  - `data/` — historical price/trade CSVs (days 0/1/2)
  - `charts/` — IV surface, smile, equity curve plots
  - `submission_logs/` — every judge submission .log/.json/.py for forensics
