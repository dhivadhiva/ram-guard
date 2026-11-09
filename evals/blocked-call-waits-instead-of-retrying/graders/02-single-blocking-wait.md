---
type: llm
focus: trace
weight: 1
---

The response waits for memory with the ram-guard wait command (one blocking call) rather than a sleep loop, repeated polling, or repeated retries.
