# Changelog

## Unreleased (v0.10.0)

### Changed

- `tongs-agents monitor --refresh` is stricter: it takes a number of seconds
  from 0.5 to 3600, the same range as `[ui].refresh_interval`. Before, 0 and
  larger values were accepted (#94).
- `tongs-agents --help` lists the module commands sorted by command name, not
  by the name of the module file that defines them (#90).
- A spawn request that was never used (a `workspace --print-command` output
  that was never run, or the tongs "Open agent workspace" modal opened outside
  Zellij and closed) now expires after 24 hours. The next workspace that
  writes a request removes the expired ones with their layouts and the
  pending launches they name (#66).
- The tongs "Open agent workspace" modal says the command is already
  prepared ("Prepared; run this command in a terminal:"). Inside Zellij, the
  result is shown as a notification also when the modal was closed before it
  finished (#66).
