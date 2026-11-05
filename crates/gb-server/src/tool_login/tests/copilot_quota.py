"""Exercise the real Copilot SDK protocol against a synthetic account."""

import asyncio

from guildbotics.guest.usage_probe import EnvironmentProcess, _CONTENT_LENGTH, _probe_request


async def main():
    process = EnvironmentProcess(
        await asyncio.create_subprocess_exec(
            "copilot", "--headless", "--stdio", "--no-auto-update",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    )
    try:
        async with asyncio.timeout(30):
            await _probe_request(
                process,
                1,
                "connect",
                {
                    "supportedTaskKinds": [],
                    "clientInfo": {"editorName": "probe", "editorVersion": "1"},
                },
                label="GitHub Copilot CLI",
                framing=_CONTENT_LENGTH,
            )
            quota = await _probe_request(
                process, 2, "account.getQuota", {},
                label="GitHub Copilot CLI", framing=_CONTENT_LENGTH,
            )
            assert "quotaSnapshots" in quota
    finally:
        await process.kill()
    print("quota protocol responded")


asyncio.run(main())
