# Skill eval workspace

Output directory for `/skill-creator` trigger evaluations of this plugin's skills (`video-intel`, `translate-bcs`).

## Layout

```
skill-eval-workspace/
├── README.md                                    ← this file
└── iteration-N/                                 ← one full eval pass per folder, manually incremented
    ├── <skill>-trigger-eval.json                ← INPUT: the test set (queries + should_trigger)
    ├── <skill>-results.json                     ← OUTPUT: raw per-query trigger rates  [gitignored]
    ├── <skill>-stderr.log                       ← debug log from run_eval.py            [gitignored]
    └── <skill>-report.html                      ← rendered HTML for browser review
```

## Lifecycle

Iterations are **manual, not automatic**. Each "edit SKILL.md → re-run eval" cycle gets a new `iteration-N/` folder. The convention mirrors how the upstream skill-creator review viewer expects to find prior runs (`generate_review.py --previous-workspace iteration-(N-1)`).

Typical flow:

1. Run trigger eval for the current SKILL.md, results land in `iteration-1/`.
2. Edit SKILL.md based on the report.
3. Run again — manually create `iteration-2/`, repeat the eval, compare scores.
4. Stop when scores plateau or the user is happy with trigger behavior.

To bump iteration: `mkdir skill-eval-workspace/iteration-N` and re-run `python -m scripts.run_eval --eval-set <path-to-trigger-eval.json> --skill-path skills/<skill> ...` with the output redirected into the new folder.

## What's in git, what's not

| File pattern | Tracked? | Why |
|---|---|---|
| `**/*-trigger-eval.json` | yes | Test sets — they define what was measured. Worth versioning. |
| `**/*-report.html` | yes | Rendered review artifact — linkable from PRs / release notes. |
| `**/*-results.json` | no | Raw per-query data — large, noisy, derivable from the eval JSON + harness. |
| `**/*-stderr.log` | no | Debug-only. Re-runnable. |
| `**/*.raw.txt` | no | Forensic sidecars. |
| `README.md` | yes | This file. |

If you need a tracked file that's currently ignored (e.g. snapshot a particularly interesting raw result), use `git add -f path/to/file`.

## Running an eval (cheat sheet)

```bash
# From the skill-creator dir, output into this workspace
cd ~/.claude/skills/skill-creator
python -m scripts.run_eval \
  --eval-set /path/to/skill-eval-workspace/iteration-N/<skill>-trigger-eval.json \
  --skill-path /path/to/skills/<skill> \
  --num-workers 6 --runs-per-query 3 --timeout 120 --verbose \
  > /path/to/skill-eval-workspace/iteration-N/<skill>-results.json \
  2> /path/to/skill-eval-workspace/iteration-N/<skill>-stderr.log
```

To regenerate the HTML report from existing results:

```bash
cd ~/.claude/skills/skill-creator
python -c "
import json
from scripts.generate_report import generate_html
data = json.load(open('PATH/<skill>-results.json'))
wrapped = {'history': [{'iteration': 0, 'description': data['description'], 'results': data['results']}], 'holdout': 0}
open('PATH/<skill>-report.html', 'w').write(generate_html(wrapped, skill_name='<skill>'))
"
```

## Known caveats on Windows

- `run_eval.py`'s `find_project_root()` walks up looking for `.claude/`. From `~/.claude/skills/skill-creator/` it lands at `~/`, so `claude -p` runs with no project context. Trigger-rate numbers are systematic underestimates compared to real interactive Claude Code sessions where the project is loaded.
- See [`work/2026-04-16/05-skill-creator-eval-windows-debug.md`](../work/2026-04-16/05-skill-creator-eval-windows-debug.md) for the full investigation and the three upstream bugs patched.
