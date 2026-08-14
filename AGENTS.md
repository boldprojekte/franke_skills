# AGENTS.md

## Release

"Mach einen Release" heißt alle fünf Schritte, nicht nur den Commit:

1. `VERSION` in `skills/engineering/cxcc-subagent/scripts/cdx.py` hochziehen
2. CHANGELOG.md: neuer `## X.Y.Z (YYYY-MM-DD)`-Abschnitt ganz oben
3. Commit `feat: <was>, release X.Y.Z` (deutsch, Git-Identität des Users, keine Anthropic-Trailer)
4. `git tag vX.Y.Z && git push origin main && git push origin vX.Y.Z` — die Tags sind lightweight, `--follow-tags` greift nicht
5. `gh release create vX.Y.Z --title "vX.Y.Z (YYYY-MM-DD)" --notes-file <changelog-abschnitt>`

Ohne Schritt 5 sieht der User im Repo keinen Release. Jede Version hat ein GitHub-Release-Objekt, Titel und Body folgen dem Muster von `gh release view v0.8.0`.

Vor dem Commit: `python3 -m unittest discover -s skills/engineering/cxcc-subagent/scripts/tests -q` (kein pytest im Environment).
