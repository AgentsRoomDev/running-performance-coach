# The trigger prompt

> 📖 **New here?** Start with the walkthrough — [How I built my own AI running coach](https://agentsroom.dev/blog/build-your-own-ai-running-coach)

This is the message handed to the coach agent when a session lands. Paste it into
the **Prompt** field of your AgentsRoom webhook trigger.

It is the third and last configuration layer:

| Layer | Where | Answers |
|---|---|---|
| Persona | AgentsRoom, system prompt | *who* the coach is |
| [`CLAUDE.md`](../CLAUDE.md) | the repository | *how it works here* |
| **This prompt** | AgentsRoom, trigger | *what to do right now* |

## The variables

The trigger receives the JSON body sent by `strava_publish.py` and exposes it as
template variables. The payload contract is exactly
`{type, title, body, author, url, id}` — the endpoint maps them to:

| Variable | Contains |
|---|---|
| `{{event.title}}` | `03/09 — 5 x 1000m r' 2'` |
| `{{event.body}}` | the session summary: reps, splits, HR on the efforts, volume, planned session code |
| `{{event.url}}` | `https://www.strava.com/activities/20018212438` |
| `{{event.id}}` | the Strava activity id |

⛔ **The plan's text never enters `{{event.body}}`** — the same rule as for
published descriptions. The webhook carries the *code* of the planned session and
the *path* to the sheets. An agent that has the repository will read them itself;
an agent that does not has no business receiving internal instructions.

> 💡 A webhook prompt that does not mention its event forces the agent to go
> looking for the data — which is exactly the polling this feature removes.

## Things this prompt learned the hard way

Each of these lines exists because something broke without it.

- **"Nobody will read a question."** An unattended run has no one to answer. An
  agent that asks for arbitration just stops.
- **Pin the browser.** With two Chrome extensions connected, nothing guarantees
  which one the agent gets — and only one holds the Strava session. Selecting it
  by device id is not persisted across sessions, so it belongs in the prompt.
- **Don't rewrite an existing analysis.** Otherwise a replay overwrites work.
- **One coach comment per activity.** Otherwise a replay spams the activity.
- **The comment field has no `maxlength`.** Nothing stops an agent writing too
  long; the server rejects it on submit. The prompt has to enforce brevity.
- **Public vs private.** A Strava comment is public. The email is not. The prompt
  states explicitly what may cross into each.

---

## The prompt

Replace `{{…}}` placeholders that are **not** `{{event.*}}` — those are yours.

````markdown
New session imported from Strava.

**{{event.title}}** — activity `{{event.id}}`
{{event.url}}

{{event.body}}

---

You are in the `{{REPO_NAME}}` repository, on {{ATHLETE_NAME}}'s machine.
**Read `CLAUDE.md` first**: it is the law. You write in {{LANGUAGE}} and you
address {{ATHLETE_NAME}} directly throughout (§3).

The §6 ritual applies, but **its step 1 is already done**: `strava_publish.py`
created the session sheet and committed it. You resume at step 2 and go all the
way. Three deliverables, in this order: **the analysis in the repository**, **the
comment under the Strava activity**, **the email**.

⚠️ **You are running unattended: nobody will read a question.** Never ask for
arbitration — you decide, you act, and you say in your report what you settled
and why.

---

## 1 · Analyse and adjust the plan (§6 ritual, steps 2 to 6)

1. `git pull --rebase` first: the sheet may come from the server.
2. Read, in this order: today's sheet in `journal/{{YEAR}}/`, the week sheet
   `plan/weeks/{{YEAR}}-Wnn.md`, `athlete/zones-and-paces.md`, and **the last 3
   session sheets** — a session is never judged alone (accumulated load,
   sequencing, ongoing watch items).
3. Write the `## Analysis` section of today's sheet: **verdict first** (on target
   / above / below, and by how much), then the signals that carry it, then what
   it changes.
   ⛔ **If `## Analysis` is already filled, do not rewrite it** (§10: we do not
   rewrite history). Read it, use it, and go straight to block 2.
4. Update the week sheet: tick the session, note actual vs planned, and trace
   **every** plan change under `## Adjustments`, with its reason.
5. **Regenerate the `README.md`** (§5). Not optional: it is the screen
   {{ATHLETE_NAME}} reads on their phone.
6. Commit + push (§7). Explicit paths, ⛔ never `git add -A`.

## 2 · Kudos and comment on Strava

The goal: that **the coach's verdict is readable under the activity, from a
phone, without opening the repository**. This is not self-congratulation — a
comment that says "well done" is worthless. We want the verdict, the number that
carries it, and what it changes.

0. 🔒 **PICK THE BROWSER BEFORE ANY ACTION.** If several Claude in Chrome
   extensions are connected, nothing guarantees which one you get. Call
   `select_browser` with device id **`{{CHROME_DEVICE_ID}}`** — the one holding
   both the Strava session and the webmail session.
   ⛔ Do **not** call `list_connected_browsers` to make the user choose, and ask
   no questions: you are unattended, nobody will answer and you will hang. If
   `select_browser` fails on that id, note it in your report and stop block 2
   rather than working in the wrong browser.

Then, with Claude in Chrome (`tabs_context_mcp` first, then a fresh tab):

1. Open `https://www.strava.com/`. **The session should already be open on the
   `{{COACH_STRAVA_ACCOUNT}}` account** — if so touch nothing and go to step 3.

2. If it expired: "Log in", enter `{{COACH_STRAVA_ACCOUNT}}`, and ask for a
   **login code by email**. Fetch it from `{{WEBMAIL_URL}}` (session already
   open) in a second tab, take the code from the **most recent** message, come
   back to the Strava tab and enter it.

3. Open `{{event.url}}`. **Check the activity belongs to {{ATHLETE_NAME}}**
   (athlete `{{STRAVA_ATHLETE_ID}}`). If the author does not match, write
   nothing and report it.

4. **Leave a kudos**: the "Give kudos" button, top right of the activity banner.
   If already given, the button no longer offers it — do not force it.

5. Open the comments: **"View all comments"** (same banner). A dialog opens with
   "Kudos" / "Comments" tabs and, at the bottom, the *"Add a comment"* field and
   **"Post"**.

6. ⛔ **One coach comment per activity.** The "Comments" tab lists what is there:
   **if a comment from `{{COACH_STRAVA_ACCOUNT}}` is already present, do not add
   a second one** and move to block 3 — including on a replay.

7. **Post ONE comment**, in {{LANGUAGE}}, addressing {{ATHLETE_NAME}} directly:

   - open with the verdict emoji — ✅ on target / ⚠️ gap to watch / ⛔ session missed;
   - the verdict in one sentence, with **the number that carries it** (pace, gap
     to target, mean HR on the efforts);
   - what it changes for the next session, in one sentence.

   **Short: one or two sentences, ~250 characters.** ⚠️ The field enforces **no
   limit in the browser**: nothing will stop you writing too long, and it is
   **Strava that refuses on submit**. So count before you type. If "Post" fails,
   **shorten and repost** — ⛔ never split into two comments.

   ⛔ **A Strava comment is PUBLIC.** Same rule as for descriptions (§8): no
   target HR, no niggle or pain, no internal trade-off, no link to the repository
   (it is private), no predicted finishing time.

8. Check the comment actually appeared in the list, then close the tabs you
   opened.

## 3 · Email the report

From the webmail already open (`{{WEBMAIL_URL}}`), write to
**{{ATHLETE_EMAIL}}**.

- **Subject:** `[Coach] <DD/MM> — <short session name> — <verdict>`
- **Body:** the full analysis, the one you just wrote in the sheet. This is a
  private channel: here you may include HR, niggles and trade-offs, unlike the
  Strava comment.
- Plain text, no attachment.

## 4 · Report back

Finish in **5 lines maximum**: the verdict, what changed in the plan, the files
pushed, the **exact text** of the comment you posted, and the state of the email.

If a step failed (expired session, code not found, comment refused, push
rejected), **say so plainly** rather than working around it or passing over it in
silence.
````
