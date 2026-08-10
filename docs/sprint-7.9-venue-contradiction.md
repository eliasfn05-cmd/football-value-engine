# Sprint 7.9 — Venue-Specific Contradiction Guard

Sprint 7.9 gives more weight to the exact match context: home-team results at home and away-team results away.

## Goal

Prevent a very strong profile on one side from dragging a weak venue-specific profile into Premium Value. This specifically targets false positives such as an Over 2.5 candidate where the home side has only 2/5 recent home Overs while the visitor has a very high away Over rate.

## Rules

- Requires at least five recent venue-specific matches on both sides before applying the hard contradiction veto.
- Uses recent home/away rates as the primary signal and the 10-match venue baseline as confirmation.
- Severe contradiction: weak recent side <= 40%, weak long venue baseline <= 50%, strong opposite recent side >= 70%.
- Severe contradiction is a hard Premium veto for both Over 2.5 and BTTS.
- Moderate venue weakness applies a progressive reliability and Premium-rank penalty rather than an automatic rejection.
- BTTS receives an additional veto when the weak venue side also has a high recent failed-to-score rate.
- Existing Sprint 7.6 fragile-Over and Sprint 7.7 market-disagreement guards remain active, so Santos–Athletico-type profiles keep their existing protection while Sprint 7.9 adds the missing Mura–Radomlje-style venue asymmetry protection.

## Audit

Premium rationale now records `venue_specific_contradiction`, `venue_rank_penalty`, and `effective_reliability_after_venue`. Rejection diagnostics expose `venue_specific_contradiction` when the hard veto fires.
