# Fireborn Guard

A free spend cap and loop breaker for Claude Code, shipped as a plugin.

Interactive Claude Code has no hard dollar stop of its own. A long session or a stuck agent can keep calling the same tool until your plan or your API bill runs out. Fireborn Guard checks every tool call before it runs and says no once a session costs too much or starts looping.

## The full Fireborn app

This plugin is the small free part. The full Fireborn app is the paid product and does a lot more:

- Token saving modes that cut what a session spends, not just stop it
- A detailed token bill: where every dollar of a session went
- A dashboard with today's spend, your caps and the most wasteful calls
- Windows desktop app

It is still being tested, so there is no download yet. See [abwd.vercel.app](https://abwd.vercel.app) for what it does and when it is ready.

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

Needs `python3` on your PATH. No account, no network calls.

## Privacy

Fireborn Guard never sees your API key or password. It only reads the session transcript Claude Code already keeps on your machine, to count tokens and spot repeated tool calls. Repeats are tracked as short hashes, not your text. It makes no network calls and there is no server.

## What it does

**Spend cap.** It estimates what the session has used at API list price. Past the soft line ($5 by default) Claude is told the session is getting expensive. Past the hard line ($15 by default) further tool calls are denied. A model it does not know is priced at the most expensive known rate, never at $0.

**Loop breaker.** The same tool with the same input gets a warning at 4 in a row and a stop at 8. A shell command failing over and over gets a warning after 3 and a stop after 5. `sleep`, `wait`, `watch`, `tail -f` and `timeout` never count as a loop.

**Letting a session continue.** When a session is stopped, the message shows a command to run in your own terminal that lets that one session go on. Claude itself cannot run it.

## Settings

Set these in the shell you start Claude Code from. Set any of them to 0 to turn that check off.

| Variable | Default |
| --- | --- |
| `FIREBORN_SOFT_USD` | 5 |
| `FIREBORN_HARD_USD` | 15 |
| `FIREBORN_SAME_WARN` / `FIREBORN_SAME_STOP` | 4 / 8 |
| `FIREBORN_FAIL_WARN` / `FIREBORN_FAIL_STOP` | 3 / 5 |

On a Pro or Max subscription you are not charged the dollar figure. Read it as how much usage the session has burned.

## Limits

- It can only stop tool calls. A session that only talks is not stopped.
- The estimate can trail the newest reply by a moment.
- Tested on Linux in real Claude Code sessions against a local stand-in for the model API. Not yet run against the real Anthropic API, and not yet tried on Windows or macOS.

## License

MIT. See `LICENSE` and `NOTICE`.
