# Training log

> 📖 **New here?** Start with the walkthrough — [How I built my own AI running coach](https://agentsroom.dev/blog/build-your-own-ai-running-coach)

**This folder is the project's source of truth.** The plan says what was
intended; the log says what was done. When they contradict each other, the log
wins.

---

## Convention

```
journal/
└── YYYY/                    ← one year
    └── YYYY-MM-DD.md        ← one sheet per training day
```

- **One day = one file.** ISO name, no exceptions.
- **Two sessions on the same day** → a single file with two sections,
  `## Session 1` and `## Session 2`.
- **Rest day** → no file. The absence *is* the information.
- Template: [`templates/session.md`](../templates/session.md).

## Filling rules

1. **You record what happened, not what should have happened.** A session cut
   short is recorded as cut short, with the reason.
2. **The numbers are exact.** Reported times are used as given. What is not known
   stays empty — never an estimate dressed up as a measurement.
3. **A sheet already written is not rewritten.** You complete it (Strava data
   that arrived later, next-day feel); you do not erase it.
4. **The `Analysis` field is mandatory.** A session without a verdict is useless.

Rule 2 is the one that breaks first, and it matters most. A coach reasoning on
invented heart rates will confidently give you the wrong plan.

## History before the project

Sessions predating the start of your log do not need individual sheets: they can
live in the Strava export and in the aggregates under
[`data/derived/`](../data/derived/). The detailed log starts with the project.

Sheets created automatically by `scripts/strava_publish.py` carry **measured data
only** — their `Analysis` section stays empty until the coach fills it. That
separation is deliberate: importing is not coaching.

## Finding a session

```bash
# Every threshold session
grep -rl "THR" journal/

# Every session in one month
ls journal/2026/2026-09-*

# Every sheet mentioning pain
grep -ril "pain" journal/
```
