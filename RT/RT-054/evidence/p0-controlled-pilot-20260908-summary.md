# RT-054 P0 controlled pilot summary

One local in-process, FileStation-backed cold diagnostic was attempted through
`GatewayApp` with P0 diagnostics enabled.  A write trap rejected every backend
write method; its counter remained zero.  The request returned the sanitized
error category `lexical_unavailable` after six observed download attempts, so
there is no valid successful sample and no protected source data in this
evidence.

The cold request took 24.122983625 seconds and observed 1,496,819,471 payload
bytes across downloads.  The diagnosis stopped there: replaying hot, the other
library, or a 20-sample matrix would turn a failed large-payload read into
avoidable NAS load.  P50/P95 and physical/wire budget candidates are therefore
not computable; `wire_bytes` remains unknown.  This sample does not establish a
root cause, but it falsifies any claim that this session collected the required
successful P0 performance matrix.  P1b remains NO-GO and requires a new
solution gate with a segmented sampling plan that separates network download,
offline parsing, and scoring.
