# `openshell rule get` text fixtures

These files are what `openshell rule get <sandbox> --status pending` prints,
built by hand from the upstream source, not captured from a live gateway:
`sandbox_draft_get` in `crates/openshell-cli/src/run.rs` at upstream
[`ba302a6`](https://github.com/NVIDIA/OpenShell/blob/ba302a6b0481ae02b647a5c84302aa201199f9da/crates/openshell-cli/src/run.rs#L6731-L6839)
(v0.1.6). The values are illustrative. Replacing them with a capture from the
maintainer's live run is pending (docs/work/openshell-sandbox.md, row 21).

| File | Shape |
| --- | --- |
| `none.txt` | no rules |
| `pending-one.txt` | one plain proposal |
| `pending-many.txt` | three proposals with `Security:`, `Prover:`, `Hits:`, an L7 endpoint, `Candidate:` and `Application:` lines |
| `pending-colored.txt` | `pending-one.txt` with the escapes the CLI prints when forced to colour |
| `legacy-validation.txt` | the older `Validation:` label (upstream before `ba302a6`) |
| `unknown-label.txt` | a label the parser does not know; it must refuse the output |
| `injected-rationale.txt` | a rationale carrying a newline and a forged block; the chunk count gives it away |

Upstream prints a blank line after every rule, the last one included. The
files drop that final blank line so the repository's whitespace check passes;
the parser accepts the output with or without it (the inline cases in
`tests/test_sandbox_approvals.py` keep it).
