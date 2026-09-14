"""Limits shared by process boundaries and the capabilities they transport."""

# asyncio's default 64 KiB StreamReader limit aborts readline() on single-line
# JSON payloads such as replayed tool results or aggregated command output.
STREAM_READ_LIMIT = 10 * 1024 * 1024
