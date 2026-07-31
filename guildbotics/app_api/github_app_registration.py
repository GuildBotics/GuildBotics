"""Browser-facing pages for the GitHub App manifest round trip.

These pages are served without the session token (the user's browser cannot
send it); the unguessable registration state in the URL is the credential. It
stays valid until the registration expires so reloads and polling keep
working, while the one-time manifest code consumed on conversion prevents
replaying the credential exchange itself.
"""

from __future__ import annotations

import html

from fastapi.responses import HTMLResponse

from guildbotics.utils.i18n_tool import t

_PAGE_TEMPLATE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>GuildBotics</title>
<style>
body {{ font-family: system-ui, sans-serif; margin: 4rem auto; max-width: 40rem;
       text-align: center; color: #333; }}
button {{ font-size: 1rem; padding: 0.5rem 1.5rem; }}
</style>
</head>
<body>
{body}
</body>
</html>"""


def manifest_post_page(submission_url: str, manifest_json: str) -> HTMLResponse:
    """Return a page that immediately posts the manifest form to github.com."""
    body = (
        f"<p>{html.escape(t('app_api.github_app_registration.redirecting'))}</p>"
        f'<form id="manifest-form" action="{html.escape(submission_url)}" '
        'method="post">'
        f'<input type="hidden" name="manifest" value="{html.escape(manifest_json)}">'
        f'<button type="submit">'
        f"{html.escape(t('app_api.github_app_registration.submit'))}</button>"
        "</form>"
        '<script>document.getElementById("manifest-form").submit();</script>'
    )
    return HTMLResponse(_PAGE_TEMPLATE.format(body=body))


def registration_error_page(message: str) -> HTMLResponse:
    """Return an error page telling the user to retry from the app."""
    body = (
        f"<p>{html.escape(t('app_api.github_app_registration.error_title'))}</p>"
        f"<p>{html.escape(message)}</p>"
        f"<p>{html.escape(t('app_api.github_app_registration.error_hint'))}</p>"
    )
    return HTMLResponse(_PAGE_TEMPLATE.format(body=body), status_code=400)


def missing_code_error_page() -> HTMLResponse:
    """Error page for a callback that arrived without the one-time code."""
    return registration_error_page(t("app_api.github_app_registration.missing_code"))
