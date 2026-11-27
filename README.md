# Fireborn Guard

A spend cap and loop breaker for Claude Code, shipped as a plugin.

Interactive Claude Code has no hard dollar stop of its own. A long session or a stuck agent can keep calling the same tool until your plan or your API bill runs out. Fireborn Guard sits in a `PreToolUse` hook and checks every tool call before it runs.

## Why this exists: one review, 34 subagents

A Claude Code user on r/ClaudeAI (u/Substantial-Aide-66, 25 November 2026) asked the tool to review three hours of work. In their words: "i cant believe its open 34 subagents and eat almost 50% my weekly usage limit on ultra subs. 660 mill token in total in 50 minutes." ([original post](https://www.reddit.com/r/ClaudeAI/comments/1y50f53/accidentaly_nuking_my_usage_limit_in_1_hour_on/)) Those numbers are the poster's own report, not verified by us, and we do not convert them into dollars.

The top reply gave the fix in one line: "Put a number on the fan-out in the prompt itself. 'Use at most 3 subagents, and ask me before starting more' is the line that would have stopped this at 3 instead of 34." (u/tariqosmani, same thread.)

This plugin is the same idea as a hard stop instead of a prompt suggestion. The soft line ($5 by default) warns once the session gets expensive; the hard line ($15 by default) denies further tool calls; the loop breaker stops a failing command after 5 failures in a row. One honest limit: those lines are API list-price estimates in dollars, and this case burned a subscription weekly allowance in points. The two are not the same unit, so we do not claim installing the plugin would have stopped this exact bill. What the cap does stop is a session spending without anything outside the agent saying no.

```
claude plugin marketplace add 6hn68/fireborn-guard && claude plugin install fireborn-guard@fireborn
```

## Install

From your shell:

```
claude plugin marketplace add 6hn68/fireborn-guard && claude plugin install fireborn-guard@fireborn
```

Or inside a Claude Code session:

```
/plugin marketplace add 6hn68/fireborn-guard
/plugin install fireborn-guard@fireborn
```

Needs `python3` on your PATH. No other dependencies, no account, no network calls.

## What it does

**Spend cap.** Before each tool call it reads the session transcript and estimates what the session has used at API list price.

- Past the soft line ($5 by default) the call goes through, and Claude is told the session is getting expensive.
- Past the hard line ($15 by default) the call is denied.
- A model it does not know is priced at the most expensive known rate, never at $0.

**Loop breaker.**

- Same tool with the same input: a warning at 4 in a row, a stop at 8.
- Same shell command failing: a warning after 3 failures in a row, a stop after 5.
- `sleep`, `wait`, `watch`, `tail -f` and `timeout` commands never count as a loop.

**Letting a session continue.** When a session is stopped, the message shows the exact command to run in your own terminal:

```
python3 <path-to>/fireborn_guard.py allow <session-id>
```

That exempts that one session. Claude itself cannot run it; the hook refuses that command when it comes from the agent.

## Settings

Set these in the shell you start Claude Code from:

| Variable | Default |
| --- | --- |
| `FIREBORN_SOFT_USD` | 5 |
| `FIREBORN_HARD_USD` | 15 |
| `FIREBORN_SAME_WARN` / `FIREBORN_SAME_STOP` | 4 / 8 |
| `FIREBORN_FAIL_WARN` / `FIREBORN_FAIL_STOP` | 3 / 5 |

Set any of them to 0 to turn that check off.

## What the dollar figure means

It is the API list price of the tokens in the transcript (prices in `scripts/prices.json`, from Anthropic's and OpenAI's public pricing pages). On a Pro or Max subscription you are not charged that amount; read it as "how much usage this session has burned".

## Anonymous burn-pattern sharing (off by default)

```
python3 <path-to>/fireborn_guard.py share on
python3 <path-to>/fireborn_guard.py share off
```

When it is on, each stop writes one anonymous line (what kind of stop, tool name, how many repeats, a spend range such as "5-15", model family, date) to `shared-patterns.jsonl` in the plugin's data folder. No prompts, file names, commands, paths or session ids are written.

Right now nothing is uploaded. There is no server yet, and the script has no network code. If an upload is ever added, it will stay opt-in and this README will say exactly what is sent and where.

## Limits

- The hook can stop tool calls. A session that only talks, with no tool calls, is not stopped.
- Claude Code writes the transcript asynchronously, so the estimate can trail the newest reply by a moment.
- Tested on Linux. Windows and macOS have not been tried.

## How it has been tested

- 30 unit tests (`python3 -m unittest discover -s tests`).
- `claude plugin validate` passes, and install from this marketplace works in Claude Code CLI 2.1.332.
- Real Claude Code CLI 2.1.332 sessions, with the plugin installed from the marketplace, driven by a local stand-in for the model API: the hard line stopped every tool call once past $15, the soft line warned at $6 and $12, the same Bash call was warned at 4 to 7 and stopped at 8, and a failing `ls` was stopped after 5 failures.
- Not yet run against the real Anthropic API with a real model. The stand-in replays fixed tool calls, so this does not show how a real model reacts to the warnings.

## Fireborn

This plugin is the free, open part of Fireborn. Cross-vendor budgets, a burn-pattern rules library and team features are being built as the paid Fireborn product and are not in this repository.

## License

MIT. See `LICENSE` and `NOTICE`.
