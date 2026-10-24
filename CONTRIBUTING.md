# Contributing to Acroscope

Thanks for your interest. There are two kinds of contribution, code and trick data, with different terms.

## Code

The code is licensed under AGPL-3.0-or-later (see `LICENSE`). By opening a pull request you agree that your
contribution is licensed the same way (inbound = outbound).

Keep a PR focused, say what it changes and why, and don't squash: the trail of how a change was reached is part of
the record.

### AI-assisted code

Acroscope is developed with AI (Claude Code), and you are welcome to use it too. The bar stays the same though: a PR
should be high-quality code that has already been through several rounds of review and iteration on your side, with
tests, until you would stand behind every line of it as your own.

Don't open a PR with what an AI produced on the first ask and wait for me to work on it further. If I find a very
low-effort PR, I'll close it with an "AI slop" comment.

I'm flattered if you want to contribute, so let's keep good standards.

## Trick data (footage, blackbox logs, labels)

**Draft:** these terms will be finalised with the contribution feature (#71), and they are not legal advice.

To improve trick detection, pilots will be able to submit flight data: DVR footage, Betaflight blackbox logs and
their own labels ("clean roll", "sloppy", "not a loop").

By submitting trick data you:

- confirm you made the recording and own it (or otherwise have the right to submit it), and that it contains no one
  else's personal data without their consent;
- grant the Acroscope project a non-exclusive, worldwide, royalty-free licence to store, process and analyse your
  submission, including with automated and AI/ML tools, for the sole purpose of developing and improving Acroscope
  and its trick detection.

The project will:

- not publish or redistribute your raw footage or logs; only derived, aggregated improvements (detection rules,
  metrics, models) are shared;
- credit you as a trick contributor (a handle and an optional link) only if you opt in;
- remove your submission on request.

You keep all other rights to your content. This is not a public licence like CC-BY: the project gets permission to
use your data to improve the tool, not to republish it.
