"""Real child processes that emit fixed bytes, independent of installed tools."""

import sys


def process_output(
    stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0
) -> list[str]:
    return [
        sys.executable,
        "-c",
        "import sys; "
        "sys.stdout.buffer.write(bytes.fromhex(sys.argv[1])); "
        "sys.stderr.buffer.write(bytes.fromhex(sys.argv[2])); "
        "sys.exit(int(sys.argv[3]))",
        stdout.hex(),
        stderr.hex(),
        str(returncode),
    ]
