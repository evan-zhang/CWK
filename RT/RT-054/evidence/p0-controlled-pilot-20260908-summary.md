# RT-054 P0 controlled pilot summary

One local in-process, FileStation-backed cold diagnostic was attempted through
`GatewayApp` with P0 diagnostics enabled. A write trap rejected every backend
write method; its counter remained zero. The request returned the sanitized
error category `lexical_unavailable`; there is no valid successful sample and
no protected source data in this evidence.

The cold request took 24.122983625 seconds. Logical raw-index reads were 2
(1,599,012 bytes total); the logical lexical read was 1 (1,495,220,459 bytes);
the observer reported 6 unexplained download events and 1,496,819,471 successful-payload
bytes in aggregate. The latter equals the two logical payload totals, but the
old observer lacked per-attempt success/error, payload min/max/total, retry
ordinal and reason. Therefore it is unknown whether 6 denotes retries, distinct
logical downloads, observer classification, or another transport pattern; it
is also unknown whether a failed attempt transferred bytes that this observer
could not see. The equality is not proof of either no duplication or duplicate
accounting.

`json_decode` self time was 4.520568500s; BM25/span were not entered. RSS peak
was 5,461,835,776 bytes. Those facts falsify the old 256MiB per-KB, 512MiB
process and 64MiB request assumptions for this legacy payload path, but one 503
sample cannot identify a successful-path root cause. No hot replay, second
library, or 20-download matrix was run: P50/P95 and wire/physical budget are
not computable. P1b remains NO-GO. The next approved P0 gate is three-layered:
bounded network samples; isolated in-memory parsing from one controlled byte
acquisition; and n>=20 shape-equivalent, de-identified algorithm comparisons.
