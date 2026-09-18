# Owner's guide — the same system, without the jargon

**This file explains. [RUNBOOK.md](RUNBOOK.md) instructs.**

If something is broken right now and you want the command that fixes it, go to
the runbook. If you want to understand what the thing actually does, why a
decision was made, or whether the alarming message you're looking at is
serious, you're in the right place.

Written for you, not for a developer. Where a command appears here it's copied
exactly — those you do have to type character for character.

---

## What this system is, in one paragraph

It watches German-speaking startup sources — accelerator and university
websites, newsletters in your Gmail, RSS feeds, and the Memminger Zeitung
e-paper — and turns what it finds into one searchable database of startups,
with a dashboard on top. About 3,400 companies today.

**Every AI model runs on the office Mac mini.** Nothing is sent to OpenAI,
Anthropic or any other AI provider — not a sentence of it. That's a deliberate
constraint rather than a cost saving, and it's what makes the scouting data
safe to hold.

Worth stating precisely, in case you're ever asked: the machine is not sealed
off. Three things do reach the internet, and none of them is AI —

- the **nightly gap-filler** (4am) sends a *company name* to a web search
  service to find a website for records we only know by name;
- the **newspaper job** logs into the e-paper and emails the digest via Gmail;
- **collecting data** obviously means fetching public web pages.

So the accurate claim is "no AI provider ever sees our data", not "nothing
leaves the machine".

Three pieces you'll see named in logs and error messages:

| You'll see | What it actually is |
|---|---|
| **Postgres** | The database. The real record of every startup. |
| **Qdrant** | A second store holding the "meaning" of each company, so search can find *battery recycling* when the text says *Batteriespeicher*. |
| **Ollama** | The thing that runs the AI models locally. When this is busy, everything AI-related queues behind it. |

---

## Is it healthy? The four-question version

Type these four. If all four look right, nothing is wrong.

```bash
cd ~/vc-scouting-system/vc-scouting-system

curl -s http://localhost:8000/health
docker ps --format "table {{.Names}}\t{{.Status}}"
launchctl list | grep -E "vcscouting|gthub"
cat press_monitor/_last_run.json
```

What each one is really asking:

1. **"Is the system awake, and how many companies does it hold?"** You want
   `"status":"ok"` and a number. That number only ever goes up. If it went
   down, something deleted records — tell me.
2. **"Are the two databases and the search helper running?"** You want three
   lines, all saying `(healthy)`.
3. **"Is the service running?"** The line for `com.vcscouting.api` should show
   a **number** at the start. A dash means it's dead. The other two lines
   showing a dash is *normal* — those are once-a-day jobs, not services, so
   they only hold a number while actually running.
4. **"Did this morning's newspaper digest go out?"** You want `"status":
   "sent"` and today's date. `"not_published"` and `"no_matches"` are also
   fine — they mean it worked and there was nothing to send. A date older than
   today, or no file at all, means the 08:00 job didn't run.

**There is no alarm system.** Nothing will email you when something breaks.
That's the honest gap, and it's why those four commands exist. The newspaper
digest arriving is *not* proof the rest is healthy — it's a separate process
that shares almost nothing with the main system.

---

## What happens without you

Twice a week (Monday and Thursday, 5am) it does the big collection run. Every
morning at 8 it does the newspaper. Every afternoon at 1 it checks Gmail for
newsletters that arrived since the last sweep. Overnight it does tidying —
re-checking facts it's unsure about, writing plain-language explanations for
items waiting on you, and filling in gaps for companies it only knows by name.

Over a month expect about 8 big sweeps and 30 newspaper digests. The company
count grows. **The review queue also grows** — that's the one thing that needs
a human, and it's supposed to accumulate. It is not a fault.

Exact schedule: runbook §2.

---

## Three things from this week, explained properly

### "Use the latest Qwen model" — why I recommend against it

Think of the AI as two engines. A small fast one does the *reading* — pulling
company names out of a page — and runs constantly. A big slow one does the
*thinking* — judging and comparing — and runs occasionally.

You asked me to fit the newest engine. Two separate things came out of that.

**First, I found a real fault.** Newer Qwen models have a habit of "thinking
out loud" before answering. Your system knows to switch that off — but only
for the big thinking engine. Nobody ever switched it off for the small reading
engine, because the model that engine has always used doesn't have the habit.
So *any* newer model dropped into that slot would ramble until it ran out of
time and return nothing at all. It would look exactly like "the new model is
bad." That's now fixed.

**Second, the new engine still isn't worth fitting.** Once it could run, I gave
it the same text as the current one. It produced the *identical* answer and
took about three times longer. That matters because this engine runs once per
chunk of text, not once per page — a single portfolio page needed 354 runs.
About 3½ hours today; nearer 10 with the "better" model, for the same result.

So: leave the reading engine alone, and spend the effort on either of these
instead —

- **Give the current engine more time.** The one case it got wrong in my test
  was a case where it ran out of time, not one it misunderstood. Some of what
  looks like poor accuracy may just be an impatient stopwatch.
- **Put a newer model on the *thinking* engine instead.** Those runs are rare,
  so slowness costs little, and better judgement is worth more there.

Full numbers: `validation/qwen_model_ab_2026-09.md`. They're written down
because someone tested this in August and never recorded the result, so the
question had to be answered twice.

### Why the automated checks sometimes report failures that aren't real

The system has a suite of automated checks. They all run against the same
shared database, and each one tidies up before and after itself.

If **two of those suites run at the same time**, they delete each other's test
data mid-check, and you get a pile of failures that look like real bugs. The
giveaway is that the number *changes and climbs* on each run — 12, then 15,
then 54. A genuine bug fails the same way every time.

Same symptom, second cause: if something heavy is using the AI at that moment,
one of the checks can't get an answer in time and fails for that reason alone.

So before believing a scatter of failures: make sure only one run is going, and
that nothing else is hammering the AI. Runbook §"Never run two test runs at
once" has the two commands for checking.

I got this wrong myself this week and told you the checks were passing when
they weren't — I'd looked at a cut-off piece of the output that hid the
failures. The lesson is in the runbook: trust the pass/fail verdict, not a
glance at the output.

### 1,348 stale "not a duplicate" notes — waiting on your decision

When someone looks at two records and says *"these are two different
companies, stop asking me"*, the system saves a note so it never asks again.
Sensible.

**1,348 of those notes now point at companies that have since been deleted.**

Usually harmless. The catch: the system identifies a company by a fingerprint
of its name and website. So if a deleted company is discovered again later, it
comes back with the *same identity* and silently inherits the old note. The
effect would be a real duplicate that never gets flagged for you.

Not hypothetical — 11 records were deleted this week. I haven't touched these
notes, because clearing 1,348 rows of your live data is your call. Say the word
and I'll run it in preview mode first and show you exactly what would go.

### University course titles appearing as companies — fixed properly this time

Twice now, a crawl of a university site wandered into its **course catalogue**
— the page listing each subject and the professor who teaches it — and read
each subject as a company, with the professor's staff page as its "website".
That's where "Energierecht" and "Konstruktion und Entwerfen" came from.

I deleted them on the 16th and repointed that source at the university's
actual startup page. **They were back the next day.** Repointing the entry
door doesn't help if the crawler can still find its way to the catalogue from
inside.

The fix that holds is to refuse to read those pages at all — a degree
catalogue is never a startup listing, so the page itself is the reliable
signal. I did it that way rather than blocking the *names*, because any rule
broad enough to catch "Konstruktion und Entwerfen" would also catch real
company names, and the rule throws away the whole record. Blocking a page is
reversible and precise; guessing at names isn't.

Checked against all 516 pages that have ever produced a company: exactly one
is now blocked, and it's the offender.

---

## Alarming things that are completely fine

Don't spend an evening on any of these.

| What you see | Why it's fine |
|---|---|
| **A source website returning "403 Forbidden"** (startupsucht.com) | Its front page blocks us intermittently, but the system doesn't use the front page — it uses the city listing pages, which work. That source has produced 620 companies. If a health check calls it dead, the check is wrong. |
| **Run history empty after a restart** | The dashboard's history is held in memory by design. Restarting clears it. The permanent record is in the log files; nothing is actually lost. |
| **"Started — waiting for the first counters…"** | A run has begun but hasn't reported a number yet. This replaced a screen of nine zeros, which used to look like failure. Only worrying if it sits there more than a couple of minutes. |
| **Search shows results before the AI summary appears** | Deliberate. The matches take a fraction of a second; the written summary is a slow AI job that queues behind collection runs. Results first, summary when it's ready. |
| **`MuPDF error: cannot find XObject resource`** in the newspaper log | One page of some editions has a broken image in the publisher's own PDF. Not fixable from our side. Costs at most one unscanned page out of ~40. The digest still sends. |
| **Review queue getting longer** | Expected. See runbook §5. |
| **Occasional junk company names** | Fixed at the source on 18 Sep — see below. If you still see one, delete it from the dashboard and tell me which page it came from. |

---

## If something *is* wrong

Go to runbook §4 — it's organised by symptom, so find the sentence that matches
what you're seeing and follow it.

If you'd rather hand it to me, the three things that make it quick:

1. **What you saw**, copied exactly — the error text, not a paraphrase.
2. **Which of the four health commands failed**, and what it printed.
3. **Whether anything was running at the time** — a sweep, a test, a
   benchmark. Half the confusing failures this week were two things running at
   once.

---

## The rules I won't break, and why you shouldn't either

These four sit in runbook §9. They matter enough to repeat plainly:

- **No cloud AI, ever.** Everything runs on the Mac mini. This is the
  constraint the whole design rests on.
- **The `.env` file never gets committed or pasted anywhere.** It holds real
  passwords — your Gmail app password, the newspaper login, the search API key.
- **Never run the collection commands directly while the service is running.**
  Both would fight over the AI at once and you'd get the false-failure mess
  described above. Use the dashboard or the API instead.
- **Never auto-accept a value when two pending reviews disagree about it.**
  This caused a real incident in August where 130 fields across 92 companies
  were overwritten with the wrong value.

---

## Open questions for you

Nothing here is urgent, and none of it is blocking anything.

1. **The 1,348 stale notes** above — clean them up, or leave them?
2. **Should failures email you?** Right now the newspaper job records whether
   it worked, but nobody is told. The reason I didn't just build it: the digest
   already goes to colleagues, and they shouldn't receive technical failure
   notices, so someone has to decide who does. That's you.
3. **A newer model on the thinking engine?** The reading engine should stay as
   it is, but the thinking engine is a fair candidate. It'd need a couple of
   hours of measuring first.
